// Modified downstream from nginx/nginx-otel v0.1.2 for SG4.
// See MODIFICATIONS-SG4.md; this downstream work remains Apache-2.0.
#include "ngx.hpp"

#include "grpc_log.hpp"

#include "str_view.hpp"
#include "trace_context.hpp"
#include "batch_exporter.hpp"

#include <fstream>

extern ngx_module_t gHttpModule;

namespace {

struct OtelCtx {
    TraceContext parent;
    TraceContext current;

    // sg4: one logical outgoing HTTP CLIENT context for the final proxy_pass
    // operation.  It is prepared in PRECONTENT, after rewrite/access phases,
    // and is exported only when NGINX actually attempted an upstream.
    TraceContext upstream;
    uint64_t upstreamStartNs;

    // sg4: query-free snapshot of r->uri at the final PRECONTENT decision.
    // The bytes are copied into the request pool so later internal redirects
    // cannot change which path is attached to this logical CLIENT span.
    ngx_str_t upstreamPath;
    bool upstreamActive;
};

struct MainConfBase {
    ngx_str_t endpoint;
    ngx_msec_t interval;
    size_t batchSize;
    size_t batchCount;

    ngx_str_t serviceName;
};

struct MainConf : MainConfBase {
    std::map<StrView, StrView> resourceAttrs;
    bool ssl;
    std::string trustedCert;
    Target::HeaderVec headers;
};

struct SpanAttr {
    ngx_str_t name;
    ngx_http_complex_value_t value;
};

struct LocationConf {
    ngx_http_complex_value_t* trace;
    ngx_uint_t traceContext;

    ngx_http_complex_value_t* spanName;
    ngx_array_t spanAttrs;

    // sg4: inherited switch, valid in http/server/location.
    ngx_flag_t upstreamSpan;
};

char* setExporter(ngx_conf_t* cf, ngx_command_t* cmd, void* conf);
char* addResourceAttr(ngx_conf_t* cf, ngx_command_t* cmd, void* conf);
char* addSpanAttr(ngx_conf_t* cf, ngx_command_t* cmd, void* conf);
char* setTrustedCertificate(ngx_conf_t* cf, ngx_command_t* cmd, void* conf);
char* addExporterHeader(ngx_conf_t* cf, ngx_command_t* cmd, void* conf);

namespace Propagation {

const ngx_uint_t Extract = 1;
const ngx_uint_t Inject = 2;

/*const*/ ngx_conf_enum_t Types[] = {
    { ngx_string("ignore"), 0 },
    { ngx_string("extract"), Extract },
    { ngx_string("inject"), Inject },
    { ngx_string("propagate"), Extract | Inject },
    { ngx_null_string, 0 }
};

}

ngx_command_t gCommands[] = {

    { ngx_string("otel_exporter"),
      NGX_HTTP_MAIN_CONF|NGX_CONF_BLOCK|NGX_CONF_NOARGS,
      setExporter },

    { ngx_string("otel_resource_attr"),
      NGX_HTTP_MAIN_CONF|NGX_CONF_TAKE2,
      addResourceAttr },

    { ngx_string("otel_service_name"),
      NGX_HTTP_MAIN_CONF|NGX_CONF_TAKE1,
      ngx_conf_set_str_slot,
      NGX_HTTP_MAIN_CONF_OFFSET,
      offsetof(MainConfBase, serviceName) },

    { ngx_string("otel_trace"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_http_set_complex_value_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(LocationConf, trace) },

    { ngx_string("otel_trace_context"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_conf_set_enum_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(LocationConf, traceContext),
      &Propagation::Types },

    // sg4: automatically creates an HTTP CLIENT span only for locations whose
    // final content handler is ngx_http_proxy_module (proxy_pass).  The handler
    // runs in PRECONTENT; ordinary non-proxy paths do not create a CLIENT span.
    { ngx_string("otel_upstream_span"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_FLAG,
      ngx_conf_set_flag_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(LocationConf, upstreamSpan) },

    { ngx_string("otel_span_name"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_http_set_complex_value_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(LocationConf, spanName) },

    { ngx_string("otel_span_attr"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE2,
      addSpanAttr,
      NGX_HTTP_LOC_CONF_OFFSET },

      ngx_null_command
};

ngx_command_t gExporterCommands[] = {

    { ngx_string("endpoint"),
      NGX_CONF_TAKE1,
      ngx_conf_set_str_slot,
      0,
      offsetof(MainConfBase, endpoint) },

    { ngx_string("trusted_certificate"),
      NGX_CONF_TAKE1,
      setTrustedCertificate },

    { ngx_string("header"),
      NGX_CONF_TAKE2,
      addExporterHeader },

    { ngx_string("interval"),
      NGX_CONF_TAKE1,
      ngx_conf_set_msec_slot,
      0,
      offsetof(MainConfBase, interval) },

    { ngx_string("batch_size"),
      NGX_CONF_TAKE1,
      ngx_conf_set_size_slot,
      0,
      offsetof(MainConfBase, batchSize) },

    { ngx_string("batch_count"),
      NGX_CONF_TAKE1,
      ngx_conf_set_size_slot,
      0,
      offsetof(MainConfBase, batchCount) },

      ngx_null_command
};

std::unique_ptr<BatchExporter> gExporter;

StrView toStrView(ngx_str_t str)
{
    return StrView((char*)str.data, str.len);
}

ngx_str_t toNgxStr(StrView str)
{
    return ngx_str_t{str.size(), (u_char*)str.data()};
}

bool iremovePrefix(ngx_str_t* str, StrView p)
{
    if (str->len >= p.size() &&
            ngx_strncasecmp(str->data, (u_char*)p.data(), p.size()) == 0) {
        str->data += p.size();
        str->len -= p.size();
        return true;
    }

    return false;
}

MainConf* getMainConf(ngx_conf_t* cf)
{
    return static_cast<MainConf*>(
        (MainConfBase*)ngx_http_conf_get_module_main_conf(cf, gHttpModule));
}

MainConf* getMainConf(ngx_cycle_t* cycle)
{
    return static_cast<MainConf*>(
        (MainConfBase*)ngx_http_cycle_get_module_main_conf(cycle, gHttpModule));
}

LocationConf* getLocationConf(ngx_http_request_t* r)
{
    return (LocationConf*)ngx_http_get_module_loc_conf(r, gHttpModule);
}

void cleanupOtelCtx(void* data)
{
}

OtelCtx* getOtelCtx(ngx_http_request_t* r)
{
    auto ctx = (OtelCtx*)ngx_http_get_module_ctx(r, gHttpModule);

    // restore module context if it was reset by e.g. internal redirect
    if (ctx == NULL && (r->internal || r->filter_finalize)) {

        for (auto cln = r->pool->cleanup; cln; cln = cln->next) {
            if (cln->handler == cleanupOtelCtx) {
                ctx = (OtelCtx*)cln->data;
                ngx_http_set_ctx(r, ctx, gHttpModule);
                break;
            }
        }
    }

    return ctx;
}

OtelCtx* createOtelCtx(ngx_http_request_t* r)
{
    static_assert(std::is_trivially_destructible<OtelCtx>::value, "");

    auto storage = ngx_pool_cleanup_add(r->pool, sizeof(OtelCtx));
    if (storage == NULL) {
        return NULL;
    }

    storage->handler = cleanupOtelCtx;

    auto ctx = new (storage->data) OtelCtx{};
    ngx_http_set_ctx(r, ctx, gHttpModule);

    return ctx;
}

ngx_table_elt_t* findHeader(ngx_list_t* list, ngx_uint_t hash, StrView key)
{
    auto part = &list->part;
    auto elts = (ngx_table_elt_t*)part->elts;

    for (ngx_uint_t i = 0; /* void */; i++) {

        if (i >= part->nelts) {
            if (part->next == NULL) {
                break;
            }

            part = part->next;
            elts = (ngx_table_elt_t*)part->elts;
            i = 0;
        }

        if (elts[i].hash != hash || elts[i].key.len != key.size() ||
                ngx_memcmp(elts[i].lowcase_key, key.data(), key.size()) != 0) {
            continue;
        }

        return &elts[i];
    }

    return NULL;
}

StrView getHeader(ngx_http_request_t* r, StrView name)
{
    auto hash = ngx_hash_key((u_char*)name.data(), name.size());
    auto header = findHeader(&r->headers_in.headers, hash, name);

    return header ? toStrView(header->value) : StrView{};
}

ngx_int_t updateRequestHeader(ngx_http_request_t* r, ngx_table_elt_t* header)
{
    auto cmcf = (ngx_http_core_main_conf_t*)
        ngx_http_get_module_main_conf(r, ngx_http_core_module);

    auto hh = (ngx_http_header_t*)ngx_hash_find(&cmcf->headers_in_hash,
        header->hash, header->lowcase_key, header->key.len);

    return hh ? hh->handler(r, header, hh->offset) : NGX_OK;
}

ngx_int_t setHeader(ngx_http_request_t* r, StrView name, StrView value)
{
    auto hash = ngx_hash_key((u_char*)name.data(), name.size());
    auto header = findHeader(&r->headers_in.headers, hash, name);

    if (header == NULL) {
        if (value.empty()) {
            return NGX_OK;
        }

        auto headers = &r->headers_in.headers;
        if (!headers->pool && ngx_list_init(headers, r->pool, 2,
                sizeof(ngx_table_elt_t)) != NGX_OK) {
            return NGX_ERROR;
        }

        header = (ngx_table_elt_t*)ngx_list_push(headers);
        if (header == NULL) {
            return NGX_ERROR;
        }

        *header = {};
        header->hash = hash;
        header->key = toNgxStr(name);
        header->lowcase_key = header->key.data;
    }

    header->value = toNgxStr(value);
    return updateRequestHeader(r, header);
}

TraceContext extract(ngx_http_request_t* r)
{
    auto parent = getHeader(r, "traceparent");
    auto state = getHeader(r, "tracestate");

    return TraceContext::parse(parent, state);
}

ngx_int_t inject(ngx_http_request_t* r, const TraceContext& tc)
{
    auto buf = (char*)ngx_pnalloc(r->pool, TraceContext::Size);
    if (buf == NULL) {
        return NGX_ERROR;
    }

    TraceContext::serialize(tc, buf);

    auto rc = setHeader(r, "traceparent", {buf, TraceContext::Size});
    if (rc != NGX_OK) {
        return rc;
    }

    return setHeader(r, "tracestate", tc.state);
}

OtelCtx* ensureOtelCtx(ngx_http_request_t* r)
{
    auto ctx = getOtelCtx(r);
    if (ctx) {
        return ctx;
    }

    ctx = createOtelCtx(r);
    if (!ctx) {
        return NULL;
    }

    auto lcf = getLocationConf(r);
    if (lcf->traceContext & Propagation::Extract) {
        ctx->parent = extract(r);
    }

    ctx->current = TraceContext::generate(false, ctx->parent);

    return ctx;
}

ngx_int_t onRequestStart(ngx_http_request_t* r)
{
    // don't let internal redirects to override sampling decision
    if (r->internal) {
        return NGX_DECLINED;
    }

    bool sampled = false;

    auto lcf = getLocationConf(r);
    if (lcf->trace != NULL) {
        ngx_str_t trace;
        if (ngx_http_complex_value(r, lcf->trace, &trace) != NGX_OK) {
            return NGX_ERROR;
        }

        sampled = toStrView(trace) == "on" || toStrView(trace) == "1";
    }

    if (!lcf->traceContext && !sampled) {
        return NGX_DECLINED;
    }

    auto ctx = ensureOtelCtx(r);
    if (!ctx) {
        return NGX_ERROR;
    }

    ctx->current.sampled = sampled;

    ngx_int_t rc = NGX_OK;

    if (lcf->traceContext & Propagation::Inject) {
        rc = inject(r, ctx->current);
    }

    return rc == NGX_OK ? NGX_DECLINED : rc;
}

bool proxyHeaderOverridesTraceContext(const ngx_http_proxy_loc_conf_t* plcf)
{
    if (plcf == NULL || plcf->headers_source == NULL) {
        return false;
    }

    auto headers = (ngx_keyval_t*)plcf->headers_source->elts;
    for (ngx_uint_t i = 0; i < plcf->headers_source->nelts; ++i) {
        auto key = headers[i].key;
        if ((key.len == sizeof("traceparent") - 1 &&
             ngx_strncasecmp(key.data, (u_char*)"traceparent", key.len) == 0) ||
            (key.len == sizeof("tracestate") - 1 &&
             ngx_strncasecmp(key.data, (u_char*)"tracestate", key.len) == 0))
        {
            return true;
        }
    }

    return false;
}

ngx_uint_t requestHeaderCount(ngx_http_request_t* r, StrView name)
{
    auto hash = ngx_hash_key((u_char*)name.data(), name.size());
    ngx_uint_t count = 0;
    auto part = &r->headers_in.headers.part;
    auto elts = (ngx_table_elt_t*)part->elts;

    for (ngx_uint_t i = 0; /* void */; ++i) {
        if (i >= part->nelts) {
            if (part->next == NULL) {
                break;
            }
            part = part->next;
            elts = (ngx_table_elt_t*)part->elts;
            i = 0;
        }

        if (elts[i].hash == hash && elts[i].key.len == name.size() &&
            ngx_strncasecmp(elts[i].key.data, (u_char*)name.data(), name.size()) == 0)
        {
            ++count;
        }
    }

    return count;
}

bool requestTraceContextIsUnambiguous(ngx_http_request_t* r)
{
    // The official module updates the first matching request header.  If a
    // malformed request contains duplicate W3C context headers, do not create a
    // CLIENT span whose downstream parent could be parser-dependent.  This is a
    // fail-closed correctness guard; it does not change official SERVER tracing.
    return requestHeaderCount(r, "traceparent") <= 1 &&
           requestHeaderCount(r, "tracestate") <= 1;
}

bool proxyPassConfigured(const ngx_http_proxy_loc_conf_t* plcf)
{
    // This is the same pair of fields ngx_http_proxy_pass() itself uses to
    // detect whether proxy_pass is configured in the current location.
    return plcf != NULL &&
        (plcf->upstream.upstream != NULL || plcf->proxy_lengths != NULL);
}

bool upstreamAttempted(ngx_http_request_t* r)
{
    // Cache HITs return before NGINX creates/pushes an upstream state.  Requiring
    // at least one state avoids exporting phantom CLIENT spans for cached data,
    // local responses, rewrites that never proxy, and DNS failures before a peer
    // attempt is started.
    return r->upstream != NULL && r->upstream_states != NULL &&
        r->upstream_states->nelts > 0;
}

uint64_t currentUnixNano()
{
    auto now = ngx_timeofday();
    return (static_cast<uint64_t>(now->sec) * 1000 + now->msec) * 1000000;
}

void snapshotClientPathBestEffort(ngx_http_request_t* r, OtelCtx* ctx)
{
    // NGINX keeps the current normalized path in r->uri and query arguments
    // separately in r->args. Copy only r->uri: never r->unparsed_uri or args.
    // The copy makes the CLIENT attribute stable if a later internal redirect
    // changes r->uri before LOG. Allocation failure only omits url.path.
    ctx->upstreamPath.len = 0;
    ctx->upstreamPath.data = NULL;

    if (r->uri.len == 0 || r->uri.data == NULL) {
        return;
    }

    auto data = (u_char*)ngx_pnalloc(r->pool, r->uri.len);
    if (data == NULL) {
        return;
    }

    ngx_memcpy(data, r->uri.data, r->uri.len);
    ctx->upstreamPath.data = data;
    ctx->upstreamPath.len = r->uri.len;
}

void restoreServerContextBestEffort(ngx_http_request_t* r, OtelCtx* ctx)
{
    if (ctx == NULL || !ctx->current.spanId.IsValid()) {
        return;
    }

    if (inject(r, ctx->current) != NGX_OK) {
        ngx_log_error(NGX_LOG_WARN, r->connection->log, 0,
            "OTel sg4: failed to restore SERVER trace context; continuing request");
    }
}

ngx_int_t injectClientContextBestEffort(ngx_http_request_t* r,
    const TraceContext& tc)
{
    // Traceparent is required for parentage.  Tracestate is optional.  Unlike
    // the official request-start path, this additional sg4 instrumentation is
    // fail-open: telemetry must not turn a healthy proxy request into a 5xx.
    auto buf = (char*)ngx_pnalloc(r->pool, TraceContext::Size);
    if (buf == NULL) {
        return NGX_ERROR;
    }

    TraceContext::serialize(tc, buf);
    auto rc = setHeader(r, "traceparent", {buf, TraceContext::Size});
    if (rc != NGX_OK) {
        return rc;
    }

    rc = setHeader(r, "tracestate", tc.state);
    if (rc != NGX_OK) {
        ngx_log_error(NGX_LOG_WARN, r->connection->log, 0,
            "OTel sg4: failed to update tracestate; traceparent remains valid");
    }

    return NGX_OK;
}

bool postponeCurrentPrecontentHandlerToEnd(ngx_http_request_t* r)
{
    // OpenResty lua-nginx-module may move precontent_by_lua to the end of the
    // PRECONTENT phase on the first request.  A static registration order alone
    // is therefore insufficient.  Use the same phase-engine-safe pattern and
    // keep sg4 last before it decides whether the final request is proxy_pass.
    auto cmcf = (ngx_http_core_main_conf_t*)
        ngx_http_get_module_main_conf(r, ngx_http_core_module);
    auto handlers = cmcf->phase_engine.handlers;
    auto current = &handlers[r->phase_handler];
    auto last = &handlers[current->next - 1];

    if (current >= last) {
        return false;
    }

    ngx_http_phase_handler_t saved = *current;
    ngx_memmove(current, current + 1,
        (last - current) * sizeof(ngx_http_phase_handler_t));
    *last = saved;

    // ngx_http_core_generic_phase() increments phase_handler after we return
    // NGX_DECLINED.  Decrement here so the handler that moved into this slot is
    // executed next, and sg4 will be called again at the final PRECONTENT slot.
    r->phase_handler--;
    return true;
}

ngx_int_t onUpstreamPrepare(ngx_http_request_t* r)
{
    // Do not make a trace decision until every other PRECONTENT handler has run.
    // This specifically covers core try_files and OpenResty precontent_by_lua,
    // including lua-nginx-module's own first-request phase postponement.
    if (postponeCurrentPrecontentHandlerToEnd(r)) {
        return NGX_DECLINED;
    }

    // sg4 intentionally does not instrument NGINX subrequests.  Their lifecycle
    // and parent request context are different enough that pretending they are a
    // normal proxy CLIENT span can create broken trees.  The main request and
    // main-request internal redirects are supported.
    if (r != r->main) {
        return NGX_DECLINED;
    }

    auto ctx = getOtelCtx(r);
    if (ctx == NULL || !ctx->current.sampled) {
        return NGX_DECLINED;
    }

    auto lcf = getLocationConf(r);
    auto plcf = (ngx_http_proxy_loc_conf_t*)
        ngx_http_get_module_loc_conf(r, ngx_http_proxy_module);

    bool eligible = lcf->upstreamSpan &&
        (lcf->traceContext & Propagation::Inject) &&
        proxyPassConfigured(plcf) &&
        plcf->upstream.pass_request_headers &&
        requestTraceContextIsUnambiguous(r) &&
        !proxyHeaderOverridesTraceContext(plcf);

    if (!eligible) {
        if (ctx->upstreamActive) {
            // A PRECONTENT handler may run again after an internal redirect.
            // Never leave an old CLIENT id active for a new final location.
            ctx->upstreamActive = false;
            restoreServerContextBestEffort(r, ctx);
        }
        return NGX_DECLINED;
    }

    if (!ctx->upstream.spanId.IsValid()) {
        ctx->upstream = TraceContext::generate(ctx->current.sampled, ctx->current);
    }

    // Reset the start marker on the final PRECONTENT pass.  The span ends in the
    // log phase; duration therefore includes the complete proxied operation and
    // can slightly exceed pure socket-upstream time when streaming to the client.
    ctx->upstreamStartNs = currentUnixNano();
    snapshotClientPathBestEffort(r, ctx);

    if (injectClientContextBestEffort(r, ctx->upstream) != NGX_OK) {
        ctx->upstreamActive = false;
        restoreServerContextBestEffort(r, ctx);
        ngx_log_error(NGX_LOG_WARN, r->connection->log, 0,
            "OTel sg4: failed to inject CLIENT traceparent; CLIENT span disabled");
        return NGX_DECLINED;
    }

    ctx->upstreamActive = true;
    return NGX_DECLINED;
}

StrView getClientSpanName(ngx_http_request_t* r)
{
    auto u = r->upstream;
    if (u != NULL && u->upstream != NULL && u->upstream->host.len) {
        // Named/static upstreams are configuration-owned and bounded.  Do not
        // use a dynamically resolved request host here: a variable proxy_pass can
        // derive it from request input and turn the span name into high
        // cardinality telemetry.
        return toStrView(u->upstream->host);
    }
    return StrView("HTTP upstream", sizeof("HTTP upstream") - 1);
}

bool upstreamStateHasHttpResponse(const ngx_http_upstream_state_t& state)
{
    // NGINX may populate state.status with a synthetic 502/504 in
    // ngx_http_upstream_next() after a transport failure (for example connect
    // refused or timeout) even though no HTTP response was received from the
    // selected peer. header_time stays at the public not-observed sentinel
    // until an upstream response header is successfully processed.
    return state.status != 0 && state.header_time != (ngx_msec_t)-1;
}

ngx_uint_t getFinalUpstreamStatus(ngx_http_request_t* r)
{
    // Describe the final wire attempt only. In particular, NGINX's synthetic
    // transport-error 502/504 must not masquerade as a backend HTTP response.
    if (r->upstream_states == NULL || r->upstream_states->nelts == 0) {
        return 0;
    }

    auto states = (ngx_http_upstream_state_t*)r->upstream_states->elts;
    auto& finalState = states[r->upstream_states->nelts - 1];
    return upstreamStateHasHttpResponse(finalState) ? finalState.status : 0;
}

void addHttpStatusErrorType(BatchExporter::Span& span, ngx_uint_t status)
{
    if (status < 400) {
        return;
    }

    // OpenTelemetry HTTP CLIENT status rules treat both 4xx and 5xx as errors
    // when the instrumentation has no application-specific context.  error.type
    // is the decimal HTTP status string when a response status was received.
    char buf[NGX_INT64_LEN];
    auto begin = (u_char*)buf;
    auto end = ngx_sprintf(begin, "%ui", status);
    span.add("error.type", StrView(buf, end - begin));
    span.setError();
}

void addClientServerAttrs(BatchExporter::Span& span, ngx_http_request_t* r)
{
    auto u = r->upstream;
    if (u == NULL || u->upstream == NULL || u->upstream->host.len == 0) {
        return;
    }

    // Use only the configuration-owned/static upstream name.  Deliberately do
    // not use a request-resolved dynamic host for variable proxy_pass because it can be
    // request-controlled and create high-cardinality attributes.
    span.add("server.address", toStrView(u->upstream->host));
    if (u->upstream->port != 0) {
        span.add("server.port", static_cast<int>(u->upstream->port));
    }
}

bool validUpstreamMsec(ngx_msec_t value)
{
    return value != (ngx_msec_t)-1;
}

void addUpstreamAttemptEvents(BatchExporter::Span& span, ngx_http_request_t* r,
    uint64_t observedAtNs)
{
    if (r->upstream_states == NULL || r->upstream_states->nelts == 0) {
        return;
    }

    // Bound per-span event amplification.  Normal NGINX retry counts are small,
    // but a very large upstream group/tries setting must not create an
    // unbounded OTLP record on a hot request path.
    static const ngx_uint_t MaxAttemptEvents = 16;

    auto states = (ngx_http_upstream_state_t*)r->upstream_states->elts;
    auto count = r->upstream_states->nelts;
    auto recorded = count < MaxAttemptEvents ? count : MaxAttemptEvents;

    for (ngx_uint_t i = 0; i < recorded; ++i) {
        auto state = &states[i];
        span.addEvent("nginx.upstream.attempt", observedAtNs,
            [state, i](BatchExporter::Span::Event& event) {
                event.add("nginx.upstream.attempt", static_cast<int>(i + 1));

                if (state->peer != NULL && state->peer->len != 0 &&
                    state->peer->data != NULL)
                {
                    event.add("nginx.upstream.peer", toStrView(*state->peer));
                }

                // state.status alone is insufficient: NGINX can synthesize
                // 502/504 for transport failures before any upstream HTTP
                // response. Only an attempt with an observed response header
                // may emit http.response.status_code.
                if (upstreamStateHasHttpResponse(*state)) {
                    event.add("http.response.status_code",
                        static_cast<int>(state->status));
                }

                // NGINX uses (ngx_msec_t)-1 as the not-observed sentinel for
                // these timing fields.  Never cast the sentinel to a huge
                // positive integer in OTLP.
                if (validUpstreamMsec(state->connect_time)) {
                    event.add("nginx.upstream.connect_time_ms",
                        static_cast<int64_t>(state->connect_time));
                }
                if (validUpstreamMsec(state->header_time)) {
                    event.add("nginx.upstream.header_time_ms",
                        static_cast<int64_t>(state->header_time));
                }
                if (validUpstreamMsec(state->response_time)) {
                    event.add("nginx.upstream.response_time_ms",
                        static_cast<int64_t>(state->response_time));
                }
                if (state->response_length >= 0) {
                    event.add("nginx.upstream.response_length",
                        static_cast<int64_t>(state->response_length));
                }
                if (state->bytes_received >= 0) {
                    event.add("nginx.upstream.bytes_received",
                        static_cast<int64_t>(state->bytes_received));
                }
                if (state->bytes_sent >= 0) {
                    event.add("nginx.upstream.bytes_sent",
                        static_cast<int64_t>(state->bytes_sent));
                }
            });
    }

    if (count > recorded) {
        span.add("nginx.upstream.attempt_events_dropped",
            static_cast<int64_t>(count - recorded));
    }
}

void addDefaultClientAttrs(BatchExporter::Span& span, ngx_http_request_t* r,
    const OtelCtx* ctx, uint64_t observedAtNs)
{
    // sg4 uses the stable HTTP semantic attribute names for the extra CLIENT
    // span.  It intentionally does NOT dual-write the old experimental names.
    // The full-URL semantic attribute remains omitted to avoid duplicating request query strings/tokens;
    // url.path is a documented privacy-oriented deviation for this custom module.
    span.add("http.request.method", toStrView(r->method_name));

    if (ctx != NULL && ctx->upstreamPath.len != 0 &&
        ctx->upstreamPath.data != NULL)
    {
        span.add("url.path", toStrView(ctx->upstreamPath));
    }

    addClientServerAttrs(span, r);

    if (r->upstream_states != NULL) {
        span.add("nginx.upstream.attempts",
            static_cast<int64_t>(r->upstream_states->nelts));
    }

    auto status = getFinalUpstreamStatus(r);
    if (status != 0) {
        span.add("http.response.status_code", static_cast<int>(status));
        addHttpStatusErrorType(span, status);
    } else {
        // No HTTP status was received from the final upstream attempt.  Only mark
        // the logical CLIENT span as an error when NGINX itself ended in a 5xx;
        // this avoids misclassifying ordinary downstream 4xx/client-abort cases.
        auto finalStatus = r->err_status ? r->err_status : r->headers_out.status;
        if (finalStatus >= 500) {
            span.add("error.type", "nginx.upstream.no_response");
            span.setError();
        }
    }

    // Events are recorded at LOG-phase observation time because the public
    // upstream state structure exposes per-attempt durations but not an exact
    // absolute timestamp for each attempt.  We do not fabricate a waterfall.
    addUpstreamAttemptEvents(span, r, observedAtNs);
}

StrView getServerName(ngx_http_request_t* r)
{
    auto cscf = (ngx_http_core_srv_conf_t*)
        ngx_http_get_module_srv_conf(r, ngx_http_core_module);

    auto name = cscf->server_name;
    if (name.len == 0) {
        name = r->headers_in.server;
    }

    return toStrView(name);
}

void addDefaultAttrs(BatchExporter::Span& span, ngx_http_request_t* r)
{
    // based on trace semantic conventions for HTTP from 1.16.0 OTel spec

    span.add("http.method", toStrView(r->method_name));

    span.add("http.target", toStrView(r->unparsed_uri));

    auto clcf = (ngx_http_core_loc_conf_t*)
        ngx_http_get_module_loc_conf(r, ngx_http_core_module);
    if (clcf->name.len) {
        span.add("http.route", toStrView(clcf->name));
    }

    span.add("http.scheme", r->connection->ssl ? "https" : "http");

    auto protocol = toStrView(r->http_protocol);
    if (protocol.size() > 5) { // "HTTP/"
        span.add("http.flavor", protocol.substr(5));
    }

    if (r->headers_in.user_agent) {
        span.add("http.user_agent", toStrView(r->headers_in.user_agent->value));
    }

    auto received = r->headers_in.content_length_n;
    span.add("http.request_content_length", received > 0 ? received : 0);

    auto sent = r->connection->sent - (off_t)r->header_size;
    span.add("http.response_content_length", sent > 0 ? sent : 0);

    auto status = r->err_status ? r->err_status : r->headers_out.status;
    if (status) {
        span.add("http.status_code", static_cast<int>(status));

        if (status >= 500) {
            span.setError();
        }
    }

    span.add("net.host.name", getServerName(r));

    if (ngx_connection_local_sockaddr(r->connection, NULL, 0) == NGX_OK) {
        auto port = ngx_inet_get_port(r->connection->local_sockaddr);
        auto defaultPort = r->connection->ssl ? 443 : 80;

        if (port != defaultPort) {
            span.add("net.host.port", port);
        }
    }

    span.add("net.sock.peer.addr", toStrView(r->connection->addr_text));
    span.add("net.sock.peer.port", ngx_inet_get_port(r->connection->sockaddr));
}

StrView getSpanName(ngx_http_request_t* r)
{
    auto lcf = getLocationConf(r);

    if (lcf->spanName) {
        ngx_str_t result;
        if (ngx_http_complex_value(r, lcf->spanName, &result) != NGX_OK) {
            throw std::runtime_error("failed to compute complex value");
        }

        return toStrView(result);
    } else {
        auto clcf = (ngx_http_core_loc_conf_t*)
            ngx_http_get_module_loc_conf(r, ngx_http_core_module);

        return toStrView(clcf->name);
    }
}

void addCustomAttrs(BatchExporter::Span& span, ngx_http_request_t* r)
{
    auto lcf = getLocationConf(r);
    auto attrs = (SpanAttr*)lcf->spanAttrs.elts;

    for (ngx_uint_t i = 0; i < lcf->spanAttrs.nelts; i++) {
        ngx_str_t value;
        if (ngx_http_complex_value(r, &attrs[i].value, &value) != NGX_OK) {
            throw std::runtime_error("failed to compute complex value");
        }

        StrView name = toStrView(attrs[i].name);
        if (startsWith(name, "http.request.header.") ||
            startsWith(name, "http.response.header."))
        {
            //TODO: remove this once headers are supported natively
            span.addArray(name, toStrView(value));
        } else {
            span.add(name, toStrView(value));
        }
    }
}

ngx_int_t onRequestEnd(ngx_http_request_t* r)
{
    auto ctx = getOtelCtx(r);
    if (!ctx || !ctx->current.sampled) {
        return NGX_DECLINED;
    }

    auto now = ngx_timeofday();

    auto toNanoSec = [](time_t sec, ngx_msec_t msec) -> uint64_t {
        return (sec * 1000 + msec) * 1000000;
    };
    try {
        // Export CLIENT only if the final proxy module actually began at least
        // one upstream attempt.  This drops cache HITs and phantom contexts.
        if (ctx->upstreamActive && ctx->upstream.spanId.IsValid() &&
            upstreamAttempted(r))
        {
            auto clientEndNs = toNanoSec(now->sec, now->msec);
            BatchExporter::SpanInfo client{
                getClientSpanName(r), ctx->upstream, ctx->current.spanId,
                ctx->upstreamStartNs,
                clientEndNs,
                BatchExporter::SpanInfo::SpanKind::CLIENT};

            bool clientOk = gExporter->add(client,
                [r, ctx, clientEndNs](BatchExporter::Span& span) {
                    addDefaultClientAttrs(span, r, ctx, clientEndNs);
                });
            if (!clientOk) {
                ngx_log_error(NGX_LOG_NOTICE, r->connection->log, 0,
                    "OTel sg4 dropped an upstream CLIENT span");
            }
        }

        // Official v0.1.2 SERVER span behavior is preserved.
        BatchExporter::SpanInfo info{
            getSpanName(r), ctx->current, ctx->parent.spanId,
            toNanoSec(r->start_sec, r->start_msec),
            toNanoSec(now->sec, now->msec),
            BatchExporter::SpanInfo::SpanKind::SERVER};

        bool ok = gExporter->add(info, [r](BatchExporter::Span& span) {
            addDefaultAttrs(span, r);
            addCustomAttrs(span, r);
        });
        if (!ok) {
            static size_t dropped = 0;
            static time_t lastLog = 0;
            ++dropped;
            if (lastLog != ngx_time()) {
                lastLog = ngx_time();
                ngx_log_error(NGX_LOG_NOTICE, r->connection->log, 0,
                    "OTel dropped records: %uz", dropped);
            }
        }
    } catch (const std::exception& e) {
        ngx_log_error(NGX_LOG_ERR, r->connection->log, 0,
            "OTel failed to add span: %s", e.what());
        return NGX_ERROR;
    }

    return NGX_DECLINED;
}

ngx_int_t initModule(ngx_conf_t* cf)
{
    auto cmcf = (ngx_http_core_main_conf_t*)ngx_http_conf_get_module_main_conf(
        cf, ngx_http_core_module);

    auto h = (ngx_http_handler_pt*)ngx_array_push(
        &cmcf->phases[NGX_HTTP_REWRITE_PHASE].handlers);
    if (h == NULL) {
        return NGX_ERROR;
    }

    *h = onRequestStart;

    // Register in PRECONTENT.  onUpstreamPrepare() dynamically verifies that it
    // is the final handler in this phase before instrumenting.  This is required
    // because OpenResty precontent_by_lua can reorder itself at request time.
    h = (ngx_http_handler_pt*)ngx_array_push(
        &cmcf->phases[NGX_HTTP_PRECONTENT_PHASE].handlers);
    if (h == NULL) {
        return NGX_ERROR;
    }
    *h = onUpstreamPrepare;
    h = (ngx_http_handler_pt*)ngx_array_push(
        &cmcf->phases[NGX_HTTP_LOG_PHASE].handlers);
    if (h == NULL) {
        return NGX_ERROR;
    }

    *h = onRequestEnd;

    initGrpcLog();

    return NGX_OK;
}

ngx_int_t initWorkerProcess(ngx_cycle_t* cycle)
{
    auto mcf = getMainConf(cycle);

    // no 'http' or 'otel_exporter' blocks
    if (mcf == NULL || mcf->endpoint.len == 0) {
        return NGX_OK;
    }

    try {
        Target target;
        target.endpoint = std::string(toStrView(mcf->endpoint));
        target.ssl = mcf->ssl;
        target.trustedCert = mcf->trustedCert;
        target.headers = mcf->headers;

        gExporter.reset(new BatchExporter(
            target,
            mcf->batchSize,
            mcf->batchCount,
            mcf->resourceAttrs));
    } catch (const std::exception& e) {
        ngx_log_error(NGX_LOG_CRIT, cycle->log, 0,
            "OTel worker init error: %s", e.what());
        return NGX_ERROR;
    }

    static ngx_connection_t dummy;
    static ngx_event_t flushEvent;

    flushEvent.data = &dummy;
    flushEvent.log = cycle->log;
    flushEvent.cancelable = 1;
    flushEvent.handler = [](ngx_event_t* ev) {
        try {
            gExporter->flush();
        } catch (const std::exception& e) {
            ngx_log_error(NGX_LOG_CRIT, ev->log, 0,
                "OTel flush error: %s", e.what());
        }

        auto mcf = getMainConf((ngx_cycle_t*)ngx_cycle);

        ngx_add_timer(ev, mcf->interval);
    };

    ngx_add_timer(&flushEvent, mcf->interval);

    return NGX_OK;
}

void exitWorkerProcess(ngx_cycle_t* cycle)
{
    if (!gExporter) {
        return;
    }

    try {
        gExporter->flush();
    } catch (const std::exception& e) {
        ngx_log_error(NGX_LOG_CRIT, cycle->log, 0,
            "OTel flush error: %s", e.what());
    }

    gExporter.reset();
}

char* setExporter(ngx_conf_t* cf, ngx_command_t* cmd, void* conf)
{
    auto mcf = getMainConf(cf);

    if (mcf->endpoint.len) {
        return (char*)"is duplicate";
    }

    auto cfCopy = *cf;

    cfCopy.handler = [](ngx_conf_t* cf, ngx_command_t*, void*) {
        auto name = (ngx_str_t*)cf->args->elts;

        for (auto cmd = gExporterCommands; cmd->name.len; cmd++) {

            if (ngx_strcmp(name->data, cmd->name.data) != 0) {
                continue;
            }

            if (cf->args->nelts != static_cast<unsigned>(ffs(cmd->type))) {
                ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                    "invalid number of arguments in \"%V\" "
                    "directive of \"otel_exporter\"", name);
                return (char*)NGX_CONF_ERROR;
            }

            auto rv = cmd->set(cf, cmd, cf->handler_conf);

            if (rv == NGX_CONF_OK) {
                return rv;
            }

            if (rv != NGX_CONF_ERROR) {
                ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                    "\"%V\" directive of \"otel_exporter\" %s", name, rv);
            }

            return (char*)NGX_CONF_ERROR;
        }

        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
            "unknown directive \"%V\" in \"otel_exporter\"", name);
        return (char*)NGX_CONF_ERROR;
    };

    cfCopy.handler_conf = mcf;

    auto rv = ngx_conf_parse(&cfCopy, NULL);
    if (rv != NGX_CONF_OK) {
        return rv;
    }

    if (iremovePrefix(&mcf->endpoint, "https://")) {
        mcf->ssl = true;
    } else {
        iremovePrefix(&mcf->endpoint, "http://");
    }

    if (mcf->endpoint.len == 0) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
            "\"otel_exporter\" requires \"endpoint\"");
        return (char*)NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}

char* addResourceAttr(ngx_conf_t* cf, ngx_command_t* cmd, void* conf)
{
    auto mcf = getMainConf(cf);

    try {
        auto args = (ngx_str_t*)cf->args->elts;
        mcf->resourceAttrs[toStrView(args[1])] = toStrView(args[2]);
    } catch (const std::exception& e) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0, "OTel: %s", e.what());
        return (char*)NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}

char* setTrustedCertificate(ngx_conf_t* cf, ngx_command_t* cmd, void* conf)
{
    auto path = ((ngx_str_t*)cf->args->elts)[1];
    auto mcf = getMainConf(cf);

    if (ngx_get_full_name(cf->pool, &cf->cycle->conf_prefix, &path) != NGX_OK) {
        return (char*)NGX_CONF_ERROR;
    }

    try {
        std::ifstream file{(const char*)path.data, std::ios::binary};
        if (!file.is_open()) {
            ngx_conf_log_error(NGX_LOG_EMERG, cf, ngx_errno,
                "failed to open \"%V\"", &path);
            return (char*)NGX_CONF_ERROR;
        }
        file.exceptions(std::ios::failbit | std::ios::badbit);
        file.peek(); // trigger early error for dirs

        size_t size = file.seekg(0, std::ios::end).tellg();
        file.seekg(0);

        mcf->trustedCert.resize(size);
        file.read(&mcf->trustedCert[0], size);
    } catch (const std::exception& e) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
            "failed to read \"%V\": %s", &path, e.what());
        return (char*)NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}

char* addExporterHeader(ngx_conf_t* cf, ngx_command_t* cmd, void* conf)
{
    auto args = (ngx_str_t*)cf->args->elts;

    // don't force on users lower case name requirement of gRPC
    ngx_strlow(args[1].data, args[1].data, args[1].len);

    try {
        // validate header here to avoid runtime assert failure in gRPC
        auto name = toStrView(args[1]);
        if (!Target::validateHeaderName(name)) {
            return (char*)"has invalid header name";
        }
        auto value = toStrView(args[2]);
        if (!Target::validateHeaderValue(value)) {
            return (char*)"has invalid header value";
        }

        getMainConf(cf)->headers.emplace_back(name, value);
    } catch (const std::exception& e) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0, "OTel: %s", e.what());
        return (char*)NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}

void* createMainConf(ngx_conf_t* cf)
{
    auto cln = ngx_pool_cleanup_add(cf->pool, sizeof(MainConf));
    if (cln == NULL) {
        return NULL;
    }

    MainConf* mcf;
    try {
        mcf = new (cln->data) MainConf{};
    } catch (const std::exception& e) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0, "OTel: %s", e.what());
        return NULL;
    }

    cln->handler = [](void* data) {
        ((MainConf*)data)->~MainConf();
    };

    mcf->interval = NGX_CONF_UNSET_MSEC;
    mcf->batchSize = NGX_CONF_UNSET_SIZE;
    mcf->batchCount = NGX_CONF_UNSET_SIZE;

    return static_cast<MainConfBase*>(mcf);
}

char* initMainConf(ngx_conf_t* cf, void* conf)
{
    auto mcf = getMainConf(cf);

    ngx_conf_init_msec_value(mcf->interval, 5000);
    ngx_conf_init_size_value(mcf->batchSize, 512);
    ngx_conf_init_size_value(mcf->batchCount, 4);

    try {
        if (mcf->serviceName.data == NULL) {
            mcf->resourceAttrs.emplace("service.name", "unknown_service:nginx");
        } else {
            mcf->resourceAttrs["service.name"] = toStrView(mcf->serviceName);
        }
    } catch (const std::exception& e) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0, "OTel: %s", e.what());
        return (char*)NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}

char* addSpanAttr(ngx_conf_t* cf, ngx_command_t* cmd, void* conf)
{
    auto lcf = (LocationConf*)conf;

    if (lcf->spanAttrs.elts == NULL && ngx_array_init(&lcf->spanAttrs,
            cf->pool, 4, sizeof(SpanAttr)) != NGX_OK) {
        return (char*)NGX_CONF_ERROR;
    }

    auto attr = (SpanAttr*)ngx_array_push(&lcf->spanAttrs);
    if (attr == NULL) {
        return (char*)NGX_CONF_ERROR;
    }

    auto args = (ngx_str_t*)cf->args->elts;

    attr->name = args[1];

    ngx_http_compile_complex_value_t ccv = { cf, &args[2], &attr->value };
    if (ngx_http_compile_complex_value(&ccv) != NGX_OK) {
        return (char*)NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}

template <class Id>
ngx_int_t hexIdVar(ngx_http_request_t* r, ngx_http_variable_value_t* v,
    uintptr_t data)
{
    auto ctx = ensureOtelCtx(r);
    if (!ctx) {
        return NGX_ERROR;
    }

    auto id = (Id*)((char*)ctx + data);

    if (id->IsValid()) {
        auto size = id->Id().size() * 2;
        auto buf = (char*)ngx_pnalloc(r->pool, size);
        if (buf == NULL) {
            return NGX_ERROR;
        }

        id->ToLowerBase16({buf, size});

        v->len = size;
        v->valid = 1;
        v->no_cacheable = 0;
        v->not_found = 0;
        v->data = (u_char*)buf;

    } else {
        v->not_found = 1;
    }

    return NGX_OK;
}

ngx_int_t upstreamSpanIdVar(ngx_http_request_t* r, ngx_http_variable_value_t* v,
    uintptr_t data)
{
    using opentelemetry::trace::SpanId;
    auto ctx = getOtelCtx(r);
    if (ctx == NULL || !ctx->upstreamActive || !upstreamAttempted(r) ||
        !ctx->upstream.spanId.IsValid())
    {
        v->not_found = 1;
        return NGX_OK;
    }

    auto id = &ctx->upstream.spanId;
    auto size = id->Id().size() * 2;
    auto buf = (char*)ngx_pnalloc(r->pool, size);
    if (buf == NULL) {
        return NGX_ERROR;
    }

    id->ToLowerBase16({buf, size});
    v->len = size;
    v->valid = 1;
    v->no_cacheable = 0;
    v->not_found = 0;
    v->data = (u_char*)buf;
    return NGX_OK;
}

ngx_int_t parentSampledVar(ngx_http_request_t* r, ngx_http_variable_value_t* v,
    uintptr_t data)
{
    auto ctx = ensureOtelCtx(r);
    if (!ctx) {
        return NGX_ERROR;
    }

    v->len = 1;
    v->valid = 1;
    v->no_cacheable = 0;
    v->not_found = 0;
    v->data = (u_char*)(ctx->parent.sampled ? "1" : "0");

    return NGX_OK;
}

ngx_int_t addVariables(ngx_conf_t* cf)
{
    using namespace opentelemetry::trace;

    ngx_http_variable_t vars[] = {
        { ngx_string("otel_trace_id"), NULL, hexIdVar<TraceId>,
            offsetof(OtelCtx, current.traceId) },

        { ngx_string("otel_span_id"), NULL, hexIdVar<SpanId>,
            offsetof(OtelCtx, current.spanId) },

        { ngx_string("otel_parent_id"), NULL, hexIdVar<SpanId>,
            offsetof(OtelCtx, parent.spanId) },

        { ngx_string("otel_upstream_span_id"), NULL, upstreamSpanIdVar },

        { ngx_string("otel_parent_sampled"), NULL, parentSampledVar }
    };

    for (auto& v : vars) {
        auto var = ngx_http_add_variable(cf, &v.name, 0);
        if (var == NULL) {
            return NGX_ERROR;
        }
        var->get_handler = v.get_handler;
        var->data = v.data;
    }

    return NGX_OK;
}

void* createLocationConf(ngx_conf_t* cf)
{
    auto conf = (LocationConf*)ngx_pcalloc(cf->pool, sizeof(LocationConf));
    if (conf == NULL) {
        return NULL;
    }

    conf->trace = (ngx_http_complex_value_t*)NGX_CONF_UNSET_PTR;
    conf->traceContext = NGX_CONF_UNSET_UINT;
    conf->spanName = (ngx_http_complex_value_t*)NGX_CONF_UNSET_PTR;
    conf->upstreamSpan = NGX_CONF_UNSET;

    return conf;
}

char* mergeLocationConf(ngx_conf_t* cf, void* parent, void* child)
{
    auto prev = (LocationConf*)parent;
    auto conf = (LocationConf*)child;

    ngx_conf_merge_ptr_value(conf->trace, prev->trace, NULL);
    ngx_conf_merge_uint_value(conf->traceContext, prev->traceContext, 0);
    ngx_conf_merge_ptr_value(conf->spanName, prev->spanName, NULL);
    ngx_conf_merge_value(conf->upstreamSpan, prev->upstreamSpan, 0);

    if (conf->spanAttrs.elts == NULL) {
        conf->spanAttrs = prev->spanAttrs;
    }

    auto mcf = getMainConf(cf);

    if (mcf->endpoint.len == 0 && conf->trace) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
            "\"otel_exporter\" block is missing");
        return (char*)NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}

ngx_http_module_t gHttpModuleCtx = {
    addVariables,                       /* preconfiguration */
    initModule,                         /* postconfiguration */

    createMainConf,                     /* create main configuration */
    initMainConf,                       /* init main configuration */

    NULL,                               /* create server configuration */
    NULL,                               /* merge server configuration */

    createLocationConf,                 /* create location configuration */
    mergeLocationConf                   /* merge location configuration */
};

}

ngx_module_t gHttpModule = {
    NGX_MODULE_V1,
    &gHttpModuleCtx,                    /* module context */
    gCommands,                          /* module directives */
    NGX_HTTP_MODULE,                    /* module type */
    NULL,                               /* init master */
    NULL,                               /* init module */
    initWorkerProcess,                  /* init process */
    NULL,                               /* init thread */
    NULL,                               /* exit thread */
    exitWorkerProcess,                  /* exit process */
    NULL,                               /* exit master */
    NGX_MODULE_V1_PADDING
};
