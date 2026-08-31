# nginx-otel v0.1.2 + reviewed PR #97 concept + SG4

SG4 is a controlled OpenResty 1.31.1.1 / NGINX 1.31.1 derivative. It starts
from the exact official nginx-otel v0.1.2 source and selectively implements the
HTTP CLIENT / Service Graph goal without PR #97 peer-data or SSL-session callback
wrappers.

## Configuration

```nginx
http {
    otel_trace on;
    otel_trace_context propagate;
    otel_upstream_span on;
}
```

`otel_upstream_span` inherits through http/server/location and defaults to off.
Automatic instrumentation is limited to main-request `ngx_http_proxy_module`
`proxy_pass` operations.

## Trace model

SG4 deliberately preserves one CLIENT span id for the logical proxy operation:

```text
NGINX SERVER A
  -> NGINX CLIENT C
      -> downstream SERVER B
```

NGINX retries reuse the serialized upstream request and therefore reuse the same
outgoing traceparent. SG4 does not manufacture different CLIENT span ids after
the fact. Instead, each `ngx_http_upstream_state_t` is recorded as a Span Event
named `nginx.upstream.attempt`.

Per-attempt Event attributes can include peer, HTTP status only when an upstream response header was actually observed,
connect/header/response durations, response length and byte counters. Timing
fields with the NGINX `(ngx_msec_t)-1` not-observed sentinel are omitted. At most
16 attempt Events are recorded; additional attempts are counted in
`nginx.upstream.attempt_events_dropped`.

NGINX exposes durations but not exact absolute per-attempt timestamps in the
public upstream state. SG4 therefore timestamps Events at CLIENT LOG-phase
observation/end time and does not fabricate a waterfall.

## CLIENT HTTP attributes

SG4 uses the newer names only; there is no legacy dual-write:

```text
http.request.method
http.response.status_code   # only when final upstream response header/status was observed
server.address
server.port
url.path
error.type
nginx.upstream.attempts
```

The custom CLIENT span does not emit `http.method`, `http.status_code`,
`url.full`, `url.query`, `http.target`, `r->args` or `r->unparsed_uri`.
`url.path` is copied from final normalized `r->uri` and intentionally excludes
the query. This is a privacy-oriented deviation from full HTTP CLIENT semantic
convention coverage; path segments can still be sensitive or high-cardinality.

NGINX can synthesize upstream-state 502/504 values for transport errors before an HTTP response. SG4 therefore requires an observed upstream header_time before emitting http.response.status_code. If the final upstream attempt really returned 4xx/5xx, SG4 sets ERROR and uses the
numeric response code string as `error.type`. If the final attempt produced no
HTTP response and NGINX ends in 5xx, SG4 omits `http.response.status_code` and sets
`error.type=nginx.upstream.no_response` rather than fabricating NGINX's downstream
502/504 as an upstream response.

## Batch-reuse safety

Official v0.1.2 reuses protobuf Span messages between batches. SG4 therefore
truncates stale Span Events when a Span wrapper finishes and truncates stale
attributes inside every reused Event wrapper. Without both steps retry details
from an earlier request could leak into a later Trace.

## Correctness guards

The CLIENT context is prepared in the final PRECONTENT slot, after rewrite,
access, try_files and OpenResty precontent_by_lua ordering. SG4 exports the
CLIENT only after a real upstream state exists. It skips automatic CLIENT
instrumentation for subrequests, `proxy_pass_request_headers off`, duplicate W3C
context headers, or explicit proxy_set_header traceparent/tracestate overrides.
Instrumentation failures are fail-open for business traffic.

SG4 continues to avoid request-derived `u->resolved->host` for span naming and
server attributes. It also does not hook peer.data, get/free, set_session or
save_session.

Because SG4 reads the NGINX proxy loc-conf structure, compilation is hard-pinned
to NGINX 1.31.1. Re-audit before an OpenResty/NGINX core upgrade.

## License and downstream status

This is a downstream modification of `nginx/nginx-otel` v0.1.2. The upstream
project and this downstream modification are distributed under the Apache License,
Version 2.0. Keep the repository `LICENSE`, `MODIFICATIONS-SG4.md`,
`LICENSE-COMPLIANCE.md`, and applicable attribution notices when redistributing.

This is not an official F5/NGINX release and the Apache-2.0 license does not grant
rights to NGINX/F5 trademarks.

## Qualification

This is not an official NGINX release. Run the complete repository pytest suite,
CentOS 7.9 / Rocky 9 / Rocky 10 builders, an ASan/UBSan qualification build where
practical, and canary traffic before production. Pay special attention to retry,
connect failure, upstream 4xx/5xx, cache, keepalive, HTTPS upstream, internal
redirects and OTLP/Tempo volume.
