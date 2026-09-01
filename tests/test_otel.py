# Modified downstream from nginx/nginx-otel v0.1.2 for SG5.1.
# See MODIFICATIONS-SG5.1.md; inherited SG4/SG5 history remains documented; Apache-2.0.
from collections import namedtuple
import http.client
import niquests
import pytest
import socket
import time
import contextlib
import os
import struct
import threading
import urllib3
from opentelemetry.proto.trace.v1 import trace_pb2


NGINX_CONFIG = """
{{ globals }}

daemon off;

events {
}

http {
    {{ http_globals }}

    ssl_certificate localhost.crt;
    ssl_certificate_key localhost.key;


    # sg4 retry fixture: primary intentionally refuses connections; backup is
    # this same NGINX instance.  This produces two ngx_http_upstream_state_t
    # entries without any peer callback instrumentation.
    upstream sg4_retry_backend {
        server 127.0.0.1:18081 max_fails=0;
        server 127.0.0.1:18080 backup;
    }

    # Real HTTP 502 first, then successful 204 backup.
    upstream sg4_retry_http502_backend {
        server 127.0.0.1:18082 max_fails=0;
        server 127.0.0.1:18080 backup;
    }

    # Real HTTP 502 first, then transport failure on the final attempt.
    upstream sg4_retry_http502_then_dead {
        server 127.0.0.1:18082 max_fails=0;
        server 127.0.0.1:18081 backup max_fails=0;
    }

    otel_exporter {
        endpoint {{ endpoint or "127.0.0.1:14317" }};
        interval {{ interval or "1ms" }};
        batch_size 3;
        batch_count 3;

        {{ exporter_opts }}
    }

    otel_trace on;
    {{ upstream_span }}
    {{ resource_attrs }}

    server {
        listen       127.0.0.1:18443 ssl;
        listen       127.0.0.1:18443 quic;
        listen       127.0.0.1:18080;
        # sg4 test-only real HTTP 502 listener
        listen       127.0.0.1:18082;
        # sg5 test-only real HTTP 504 listener
        listen       127.0.0.1:18086;

        http2 on;

        server_name  localhost;

        location /ok {
            return 200 "OK";
        }

        location /err {
            return 500 "ERR";
        }

        location /custom {
            otel_span_name custom_location;
            otel_span_attr http.request.completion
                $request_completion;
            otel_span_attr http.response.header.content.type
                $sent_http_content_type;
            otel_span_attr http.request $request;
            return 200 "OK";
        }

        location /vars {
            otel_trace_context extract;
            add_header "X-Otel-Trace-Id" $otel_trace_id;
            add_header "X-Otel-Span-Id" $otel_span_id;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            add_header "X-Otel-Parent-Sampled" $otel_parent_sampled;
            return 204;
        }

        location /ignore {
            proxy_pass http://127.0.0.1:18080/notrace;
        }

        location /extract {
            otel_trace_context extract;
            proxy_pass http://127.0.0.1:18080/notrace;
        }

        location /inject {
            otel_trace_context inject;
            proxy_pass http://127.0.0.1:18080/notrace;
        }

        location /propagate {
            otel_trace_context propagate;
            proxy_pass http://127.0.0.1:18080/notrace;
        }

        location /notrace {
            otel_trace off;
            add_header "X-Otel-Traceparent" $http_traceparent;
            add_header "X-Otel-Tracestate" $http_tracestate;
            return 204;
        }


        # sg4 integration fixtures.  The switch itself is set at http{} scope
        # by the individual tests, matching the intended production config.
        location /sg4-backend {
            if ($server_port = 18082) { return 502; }
            return 204;
        }
        location /sg4-backend-404 {
            return 404;
        }
        location /sg4-proxy {
            add_header "X-Otel-Upstream-Span-Id" $otel_upstream_span_id always;
            proxy_pass http://127.0.0.1:18080/sg4-backend;
        }
        location /sg4-proxy-404 {
            proxy_pass http://127.0.0.1:18080/sg4-backend-404;
        }
        location /sg4-no-response {
            proxy_connect_timeout 100ms;
            proxy_next_upstream off;
            proxy_pass http://127.0.0.1:18081/sg4-backend;
        }
        location /sg4-rewrite {
            rewrite ^ /sg4-proxy last;
        }
        location /sg4-off {
            otel_upstream_span off;
            proxy_pass http://127.0.0.1:18080/sg4-backend;
        }
        location /sg4-noheaders {
            proxy_pass_request_headers off;
            proxy_pass http://127.0.0.1:18080/sg4-backend;
        }
        location /sg4-explicit-traceparent {
            proxy_set_header traceparent "00-11111111111111111111111111111111-2222222222222222-01";
            proxy_pass http://127.0.0.1:18080/sg4-backend;
        }
        location /sg4-access-deny {
            deny all;
            proxy_pass http://127.0.0.1:18080/sg4-backend;
        }
        location /sg4-try-files {
            try_files /sg4-file-that-does-not-exist @sg4-named-proxy;
        }
        location @sg4-named-proxy {
            # NGINX forbids a URI part on proxy_pass inside a named location.
            # Rewrite the URI in-place, then proxy without a proxy_pass URI.
            rewrite ^ /sg4-backend break;
            proxy_pass http://127.0.0.1:18080;
        }
        # Native proxy-semantics fixture. The same proxy is exercised with
        # otel_upstream_span at default off and with SG4 enabled. Normal HTTP
        # status/body/query/header behavior must stay the same.
        location /sg4-native-backend {
            add_header "X-SG4-Native-Method" $request_method always;
            add_header "X-SG4-Native-Arg" $arg_probe always;
            add_header "X-SG4-Native-Header" $http_x_sg4_native always;
            return 201 "SG4-NATIVE-OK";
        }
        location /sg4-native-proxy {
            add_header "X-Otel-Upstream-Span-Id" $otel_upstream_span_id always;
            proxy_pass http://127.0.0.1:18080/sg4-native-backend;
        }

        location /sg4-retry {
            add_header "X-Otel-Upstream-Span-Id" $otel_upstream_span_id always;
            proxy_connect_timeout 100ms;
            proxy_next_upstream error timeout http_500 http_502 http_503 http_504;
            proxy_next_upstream_tries 2;
            proxy_pass http://sg4_retry_backend/sg4-backend;
        }
        location /sg4-real-502 {
            proxy_next_upstream off;
            proxy_pass http://127.0.0.1:18082/sg4-backend;
        }
        location /sg4-retry-real-502 {
            proxy_next_upstream error timeout http_502;
            proxy_next_upstream_tries 2;
            proxy_pass http://sg4_retry_http502_backend/sg4-backend;
        }
        location /sg4-retry-real-502-then-dead {
            proxy_connect_timeout 100ms;
            proxy_next_upstream error timeout http_502;
            proxy_next_upstream_tries 2;
            proxy_pass http://sg4_retry_http502_then_dead/sg4-backend;
        }

        # SG5 failure-classification fixtures.  These keep the client-facing
        # NGINX 502/504 behavior unchanged and test telemetry only.
        location /sg5-connect-timeout {
            proxy_connect_timeout 100ms;
            proxy_next_upstream off;
            proxy_pass http://127.0.0.1:18087/sg5-blackhole;
        }
        location /sg5-read-timeout {
            proxy_connect_timeout 100ms;
            proxy_read_timeout 100ms;
            proxy_next_upstream off;
            proxy_pass http://127.0.0.1:18083/sg5-stall;
        }
        location /sg5-reset-before-header {
            proxy_next_upstream off;
            proxy_pass http://127.0.0.1:18084/sg5-reset;
        }
        location /sg5-invalid-header {
            proxy_next_upstream off;
            proxy_pass http://127.0.0.1:18085/sg5-invalid;
        }
        location /sg5-partial-header {
            proxy_next_upstream off;
            proxy_pass http://127.0.0.1:18088/sg5-partial;
        }
        location /sg5-real-504-backend {
            if ($server_port = 18086) { return 504; }
            return 204;
        }
        location /sg5-real-504 {
            proxy_next_upstream off;
            proxy_pass http://127.0.0.1:18086/sg5-real-504-backend;
        }
        # SG5.1 request-send timeout: the peer accepts the TCP connection but
        # deliberately does not read. NGINX buffers the client body first, then
        # proxy_send_timeout must fire after connect.
        location /sg51-send-timeout {
            client_max_body_size 20m;
            proxy_request_buffering on;
            proxy_send_timeout 100ms;
            proxy_read_timeout 2s;
            proxy_next_upstream off;
            proxy_pass http://127.0.0.1:18089/sg51-no-read;
        }

        # SG5.1 separator regression: two distinct proxy operations in one main
        # request create a peer==NULL separator in r->upstream_states. That
        # separator must not become a retry attempt/event.
        location /sg51-error-page {
            proxy_intercept_errors on;
            error_page 502 = @sg51-error-second;
            proxy_next_upstream off;
            proxy_pass http://127.0.0.1:18082/sg4-backend;
        }
        location @sg51-error-second {
            rewrite ^ /sg4-backend break;
            proxy_pass http://127.0.0.1:18080;
        }
    }
}

"""

TraceContext = namedtuple("TraceContext", ["trace_id", "span_id", "state"])

parent_ctx = TraceContext(
    trace_id="0af7651916cd43dd8448eb211c80319c",
    span_id="b9c7c989f97918e1",
    state="congo=ucfJifl5GOE,rojo=00f067aa0ba902b7",
)


def trace_headers(ctx):
    return (
        {
            "Traceparent": f"00-{ctx.trace_id}-{ctx.span_id}-01",
            "Tracestate": ctx.state,
        }
        if ctx
        else {"Traceparent": None, "Tracestate": None}
    )


def get_attr(span, name):
    for value in (a.value for a in span.attributes if a.key == name):
        return getattr(value, value.WhichOneof("value"))


@pytest.fixture
def client(nginx):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    with niquests.Session(multiplexed=True) as s:
        yield s


def test_http09(trace_service, nginx):

    def get_http09(host, port, path):
        with socket.create_connection((host, port)) as sock:
            sock.sendall(f"GET {path}\n".encode())
            resp = sock.recv(1024).decode("utf-8")
        return resp

    assert get_http09("127.0.0.1", 18080, "/ok") == "OK"

    span = trace_service.get_span()
    assert span.name == "/ok"


@pytest.mark.parametrize("http_ver", ["1.1", "2.0", "3.0"])
@pytest.mark.parametrize(
    ("path", "status"),
    [("/ok", 200), ("/err", 500)],
)
def test_default_attributes(client, trace_service, http_ver, path, status):
    scheme, port = ("http", 18080) if http_ver == "1.1" else ("https", 18443)
    if http_ver == "3.0":
        client.quic_cache_layer.add_domain("127.0.0.1", port)
    r = client.get(f"{scheme}://127.0.0.1:{port}{path}", verify=False)

    span = trace_service.get_span()
    assert span.name == path

    assert get_attr(span, "http.method") == "GET"
    assert get_attr(span, "http.target") == path
    assert get_attr(span, "http.route") == path
    assert get_attr(span, "http.scheme") == scheme
    assert get_attr(span, "http.flavor") == http_ver
    assert get_attr(span, "http.user_agent") == (
        f"niquests/{niquests.__version__}"
    )
    assert get_attr(span, "http.request_content_length") == 0
    assert get_attr(span, "http.response_content_length") == len(r.text)
    assert get_attr(span, "http.status_code") == status
    assert get_attr(span, "net.host.name") == "localhost"
    assert get_attr(span, "net.host.port") == port
    assert get_attr(span, "net.sock.peer.addr") == "127.0.0.1"
    assert get_attr(span, "net.sock.peer.port") in range(1024, 65536)


def test_custom_attributes(client, trace_service):
    assert client.get("http://127.0.0.1:18080/custom").status_code == 200

    span = trace_service.get_span()
    assert span.name == "custom_location"

    assert get_attr(span, "http.request.completion") == "OK"
    value = get_attr(span, "http.response.header.content.type")
    assert value.values[0].string_value == "text/plain"
    assert get_attr(span, "http.request") == "GET /custom HTTP/1.1"


def test_trace_off(client, trace_service):
    assert client.get("http://127.0.0.1:18080/notrace").status_code == 204

    time.sleep(0.01)  # wait for spans
    assert len(trace_service.batches) == 0


@pytest.mark.parametrize("parent", [None, parent_ctx])
def test_variables(client, trace_service, parent):
    r = client.get("http://127.0.0.1:18080/vars", headers=trace_headers(parent))

    span = trace_service.get_span()

    if parent:
        assert span.trace_id.hex() == parent.trace_id
        assert span.parent_span_id.hex() == parent.span_id
        assert span.trace_state == parent.state

    assert r.headers.get("X-Otel-Trace-Id") == span.trace_id.hex()
    assert r.headers.get("X-Otel-Span-Id") == span.span_id.hex()
    assert r.headers.get("X-Otel-Parent-Id") or "" == span.parent_span_id.hex()
    assert r.headers.get("X-Otel-Parent-Sampled") == ("1" if parent else "0")


@pytest.mark.parametrize("parent", [None, parent_ctx])
@pytest.mark.parametrize(
    "path", ["/ignore", "/extract", "/inject", "/propagate"]
)
def test_context(client, trace_service, parent, path):
    headers = trace_headers(parent)

    r = client.get(f"http://127.0.0.1:18080{path}", headers=headers)

    span = trace_service.get_span()

    if path in ["/extract", "/propagate"] and parent:
        assert span.trace_id.hex() == parent.trace_id
        assert span.parent_span_id.hex() == parent.span_id
        assert span.trace_state == parent.state

    if path in ["/inject", "/propagate"]:
        headers = trace_headers(
            TraceContext(
                span.trace_id.hex(),
                span.span_id.hex(),
                span.trace_state or None,
            )
        )

    assert r.headers.get("X-Otel-Traceparent") == headers["Traceparent"]
    assert r.headers.get("X-Otel-Tracestate") == headers["Tracestate"]


@contextlib.contextmanager
def sg5_fault_server(port, mode):
    ready = threading.Event()
    errors = []

    def run():
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if mode == "no_read":
                # Keep the receive window deliberately small so a buffered 16 MiB
                # request cannot disappear into the loopback socket buffers.
                server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            server.bind(("127.0.0.1", port))
            server.listen(1)
            server.settimeout(2.0)
            ready.set()
            conn, _ = server.accept()
            try:
                conn.settimeout(1.0)
                if mode == "no_read":
                    # Do not consume request bytes. proxy_send_timeout should fire
                    # while NGINX is sending the already-buffered client body.
                    time.sleep(1.0)
                    return

                try:
                    conn.recv(65536)
                except socket.timeout:
                    pass

                if mode == "stall":
                    time.sleep(0.35)
                elif mode == "reset":
                    conn.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                elif mode == "invalid_header":
                    # Valid status line followed by an invalid header field makes
                    # ngx_http_proxy_process_header return INVALID_HEADER without
                    # relying on HTTP/0.9 fallback behavior.
                    conn.sendall(b"HTTP/1.1 200 OK\r\nBad Header Without Colon\r\n\r\n")
                elif mode == "partial_header":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n")
                else:
                    raise AssertionError(f"unsupported SG5 fault mode: {mode}")
            finally:
                conn.close()
        except Exception as exc:
            errors.append(exc)
            ready.set()
        finally:
            server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(2.0), f"SG5 fault server {port} did not become ready"
    if errors:
        raise errors[0]
    try:
        yield
    finally:
        thread.join(2.0)
        assert not thread.is_alive(), f"SG5 fault server {port} did not stop"
        if errors:
            raise errors[0]


def sg51_trace_headers():
    trace_id = os.urandom(16).hex()
    parent_span_id = os.urandom(8).hex()
    return trace_id, {"traceparent": f"00-{trace_id}-{parent_span_id}-01"}


def sg51_raw_http_status(path, headers=None, method="GET", body=None, timeout=3):
    # Fault tests care about NGINX's downstream status, not niquests' multiplexing
    # behavior. A small stdlib HTTP/1.1 client makes malformed-upstream cases
    # deterministic and reads the full NGINX-generated error body before close.
    request_headers = {"Connection": "close"}
    request_headers.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", 18080, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        status = response.status
        response.read()
        return status
    finally:
        conn.close()


def sg4_http_config():
    return """
        otel_trace_context propagate;
        otel_upstream_span on;
    """


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_static_path_does_not_create_client(client, trace_service):
    assert client.get("http://127.0.0.1:18080/ok").status_code == 200
    span = trace_service.get_span()
    assert span.kind == trace_pb2.Span.SPAN_KIND_SERVER


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_proxy_parenting_and_query_redaction(client, trace_service):
    secret = "SG4-SECRET-MUST-NOT-APPEAR"
    r = client.get(f"http://127.0.0.1:18080/sg4-proxy?token={secret}")
    assert r.status_code == 204

    spans = trace_service.get_spans(3)
    server_spans = [s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_SERVER]
    client_spans = [s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT]
    assert len(server_spans) == 2
    assert len(client_spans) == 1

    frontend = next(s for s in server_spans if s.name == "/sg4-proxy")
    backend = next(s for s in server_spans if s.name == "/sg4-backend")
    outgoing = client_spans[0]

    assert frontend.trace_id == outgoing.trace_id == backend.trace_id
    assert outgoing.parent_span_id == frontend.span_id
    assert backend.parent_span_id == outgoing.span_id
    assert r.headers["X-Otel-Upstream-Span-Id"] == outgoing.span_id.hex()
    assert get_attr(outgoing, "http.response.status_code") == 204
    # SG5.1 performance gate: a normal successful upstream already has a real
    # HTTP status, so failure-classification attributes must add zero payload.
    assert get_attr(outgoing, "nginx.downstream.status_code") is None
    assert get_attr(outgoing, "nginx.upstream.outcome") is None
    assert get_attr(outgoing, "nginx.upstream.generated_status_code") is None
    assert get_attr(outgoing, "nginx.upstream.failure.type") is None
    assert get_attr(outgoing, "nginx.upstream.failure.stage") is None
    assert get_attr(outgoing, "http.request.method") == "GET"
    assert get_attr(outgoing, "http.method") is None
    assert get_attr(outgoing, "http.status_code") is None
    assert get_attr(outgoing, "url.path") == "/sg4-proxy"
    assert len(outgoing.events) == 1
    event = outgoing.events[0]
    assert event.name == "nginx.upstream.attempt"
    assert event.time_unix_nano == outgoing.end_time_unix_nano
    assert get_attr(event, "nginx.upstream.attempt") == 1
    assert get_attr(event, "http.response.status_code") == 204
    assert get_attr(event, "nginx.upstream.outcome") is None
    assert get_attr(event, "nginx.upstream.failure.type") is None
    assert get_attr(event, "nginx.upstream.failure.stage") is None
    assert "127.0.0.1:18080" in get_attr(event, "nginx.upstream.peer")
    assert get_attr(event, "nginx.upstream.response_time_ms") >= 0
    assert "?" not in get_attr(outgoing, "url.path")
    assert get_attr(outgoing, "url.full") is None
    assert get_attr(outgoing, "http.target") is None
    for attr in outgoing.attributes:
        if attr.value.WhichOneof("value") == "string_value":
            assert secret not in attr.value.string_value


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_internal_rewrite_keeps_server_client_server_chain(client, trace_service):
    assert client.get("http://127.0.0.1:18080/sg4-rewrite").status_code == 204
    spans = trace_service.get_spans(3)
    server_spans = [s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_SERVER]
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    frontend = next(s for s in server_spans if s.name == "/sg4-proxy")
    backend = next(s for s in server_spans if s.name == "/sg4-backend")
    assert outgoing.parent_span_id == frontend.span_id
    assert backend.parent_span_id == outgoing.span_id
    assert get_attr(outgoing, "url.path") == "/sg4-proxy"


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_retry_events_preserve_one_wire_client_span(client, trace_service):
    assert client.get("http://127.0.0.1:18080/sg4-retry").status_code == 204
    spans = trace_service.get_spans(3)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    backend = next(
        s for s in spans
        if s.kind == trace_pb2.Span.SPAN_KIND_SERVER and s.name == "/sg4-backend"
    )

    assert backend.parent_span_id == outgoing.span_id
    assert get_attr(outgoing, "nginx.upstream.attempts") == 2
    assert get_attr(outgoing, "http.response.status_code") == 204
    assert len(outgoing.events) == 2

    first, second = outgoing.events
    assert get_attr(first, "nginx.upstream.attempt") == 1
    assert get_attr(second, "nginx.upstream.attempt") == 2
    assert "127.0.0.1:18081" in get_attr(first, "nginx.upstream.peer")
    assert "127.0.0.1:18080" in get_attr(second, "nginx.upstream.peer")
    assert get_attr(first, "http.response.status_code") is None
    assert get_attr(first, "nginx.upstream.header_time_ms") is None
    assert get_attr(second, "http.response.status_code") == 204
    assert get_attr(second, "nginx.upstream.header_time_ms") >= 0
    assert first.time_unix_nano == outgoing.end_time_unix_nano
    assert second.time_unix_nano == outgoing.end_time_unix_nano


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_upstream_404_uses_stable_status_and_error_type(client, trace_service):
    assert client.get("http://127.0.0.1:18080/sg4-proxy-404").status_code == 404
    spans = trace_service.get_spans(3)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") == 404
    assert get_attr(outgoing, "error.type") == "404"
    assert outgoing.status.code == trace_pb2.Status.STATUS_CODE_ERROR
    assert len(outgoing.events) == 1
    assert get_attr(outgoing.events[0], "http.response.status_code") == 404


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_connect_failure_does_not_fabricate_http_status(client, trace_service):
    assert client.get("http://127.0.0.1:18080/sg4-no-response").status_code == 502
    spans = trace_service.get_spans(2)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") is None
    assert get_attr(outgoing, "error.type") == "nginx.upstream.no_response"
    assert outgoing.status.code == trace_pb2.Status.STATUS_CODE_ERROR
    assert get_attr(outgoing, "nginx.upstream.attempts") == 1
    assert len(outgoing.events) == 1
    assert get_attr(outgoing.events[0], "http.response.status_code") is None
    assert get_attr(outgoing.events[0], "nginx.upstream.header_time_ms") is None


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_real_upstream_502_is_reported_as_http_status(client, trace_service):
    assert client.get("http://127.0.0.1:18080/sg4-real-502").status_code == 502
    spans = trace_service.get_spans(3)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") == 502
    assert get_attr(outgoing, "error.type") == "502"
    assert outgoing.status.code == trace_pb2.Status.STATUS_CODE_ERROR
    assert len(outgoing.events) == 1
    event = outgoing.events[0]
    assert get_attr(event, "http.response.status_code") == 502
    assert get_attr(event, "nginx.upstream.header_time_ms") >= 0


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_real_502_then_success_preserves_attempt_statuses(client, trace_service):
    assert client.get("http://127.0.0.1:18080/sg4-retry-real-502").status_code == 204
    spans = trace_service.get_spans(4)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") == 204
    assert get_attr(outgoing, "nginx.upstream.attempts") == 2
    assert len(outgoing.events) == 2
    first, second = outgoing.events
    assert get_attr(first, "http.response.status_code") == 502
    assert get_attr(first, "nginx.upstream.header_time_ms") >= 0
    assert get_attr(second, "http.response.status_code") == 204
    assert get_attr(second, "nginx.upstream.header_time_ms") >= 0


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_real_502_then_connect_failure_uses_final_no_response(client, trace_service):
    assert client.get("http://127.0.0.1:18080/sg4-retry-real-502-then-dead").status_code == 502
    spans = trace_service.get_spans(3)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") is None
    assert get_attr(outgoing, "error.type") == "nginx.upstream.no_response"
    assert outgoing.status.code == trace_pb2.Status.STATUS_CODE_ERROR
    assert get_attr(outgoing, "nginx.upstream.attempts") == 2
    assert len(outgoing.events) == 2
    first, second = outgoing.events
    assert get_attr(first, "http.response.status_code") == 502
    assert get_attr(first, "nginx.upstream.header_time_ms") >= 0
    assert get_attr(second, "http.response.status_code") is None
    assert get_attr(second, "nginx.upstream.header_time_ms") is None


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg5_connect_refused_is_coarse_connect_error_not_fake_http_status(client, trace_service):
    trace_id, headers = sg51_trace_headers()
    assert sg51_raw_http_status("/sg4-no-response", headers=headers) == 502
    spans = trace_service.get_spans_for_trace(trace_id, 2)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") is None
    assert get_attr(outgoing, "nginx.downstream.status_code") == 502
    assert get_attr(outgoing, "nginx.upstream.outcome") == "no_response"
    assert get_attr(outgoing, "nginx.upstream.generated_status_code") == 502
    assert get_attr(outgoing, "nginx.upstream.failure.type") == "connect_error"
    assert get_attr(outgoing, "nginx.upstream.failure.stage") == "connect"
    assert get_attr(outgoing, "nginx.upstream.failure.phase") is None
    assert get_attr(outgoing, "error.type") == "nginx.upstream.no_response"


@pytest.mark.skipif(
    os.environ.get("NGX_OTEL_SG5_CONNECT_TIMEOUT") != "1",
    reason="requires CI iptables DROP on 127.0.0.1:18087",
)
@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg5_connect_timeout_is_timeout_in_connect_stage(client, trace_service):
    trace_id, headers = sg51_trace_headers()
    assert sg51_raw_http_status("/sg5-connect-timeout", headers=headers) == 504
    spans = trace_service.get_spans_for_trace(trace_id, 2)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") is None
    assert get_attr(outgoing, "nginx.downstream.status_code") == 504
    assert get_attr(outgoing, "nginx.upstream.generated_status_code") == 504
    assert get_attr(outgoing, "nginx.upstream.failure.type") == "timeout"
    assert get_attr(outgoing, "nginx.upstream.failure.stage") == "connect"


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg51_response_header_timeout_is_coarse_after_connect(client, trace_service):
    trace_id, headers = sg51_trace_headers()
    with sg5_fault_server(18083, "stall"):
        assert sg51_raw_http_status("/sg5-read-timeout", headers=headers) == 504
    spans = trace_service.get_spans_for_trace(trace_id, 2)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") is None
    assert get_attr(outgoing, "nginx.downstream.status_code") == 504
    assert get_attr(outgoing, "nginx.upstream.generated_status_code") == 504
    assert get_attr(outgoing, "nginx.upstream.failure.type") == "timeout"
    assert get_attr(outgoing, "nginx.upstream.failure.stage") == "after_connect"
    assert get_attr(outgoing, "nginx.upstream.failure.phase") is None


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg51_request_send_timeout_is_also_coarse_after_connect(client, trace_service):
    trace_id, headers = sg51_trace_headers()
    body = b"x" * (16 * 1024 * 1024)
    with sg5_fault_server(18089, "no_read"):
        assert sg51_raw_http_status(
            "/sg51-send-timeout", headers=headers, method="POST", body=body, timeout=5
        ) == 504
    spans = trace_service.get_spans_for_trace(trace_id, 2)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") is None
    assert get_attr(outgoing, "nginx.downstream.status_code") == 504
    assert get_attr(outgoing, "nginx.upstream.failure.type") == "timeout"
    assert get_attr(outgoing, "nginx.upstream.failure.stage") == "after_connect"
    # This is the critical SG5.1 truthfulness rule: public upstream state cannot
    # prove whether an after-connect timeout was send-side or response-read-side.
    assert get_attr(outgoing, "nginx.upstream.failure.phase") is None


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg5_reset_before_header_is_coarse_response_error(client, trace_service):
    trace_id, headers = sg51_trace_headers()
    with sg5_fault_server(18084, "reset"):
        assert sg51_raw_http_status("/sg5-reset-before-header", headers=headers) == 502
    spans = trace_service.get_spans_for_trace(trace_id, 2)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") is None
    assert get_attr(outgoing, "nginx.upstream.generated_status_code") == 502
    assert get_attr(outgoing, "nginx.upstream.failure.type") == "response_error"
    assert get_attr(outgoing, "nginx.upstream.failure.stage") == "after_connect"


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg51_invalid_header_is_deterministic_response_error(client, trace_service):
    trace_id, headers = sg51_trace_headers()
    with sg5_fault_server(18085, "invalid_header"):
        assert sg51_raw_http_status("/sg5-invalid-header", headers=headers) == 502
    spans = trace_service.get_spans_for_trace(trace_id, 2)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") is None
    assert get_attr(outgoing, "nginx.upstream.generated_status_code") == 502
    assert get_attr(outgoing, "nginx.upstream.failure.type") == "response_error"
    assert get_attr(outgoing, "nginx.upstream.failure.stage") == "after_connect"


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg5_partial_header_close_is_coarse_response_error(client, trace_service):
    trace_id, headers = sg51_trace_headers()
    with sg5_fault_server(18088, "partial_header"):
        assert sg51_raw_http_status("/sg5-partial-header", headers=headers) == 502
    spans = trace_service.get_spans_for_trace(trace_id, 2)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") is None
    assert get_attr(outgoing, "nginx.upstream.generated_status_code") == 502
    assert get_attr(outgoing, "nginx.upstream.failure.type") == "response_error"
    assert get_attr(outgoing, "nginx.upstream.failure.stage") == "after_connect"


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg51_real_upstream_504_has_no_redundant_failure_classification(client, trace_service):
    trace_id, headers = sg51_trace_headers()
    assert sg51_raw_http_status("/sg5-real-504", headers=headers) == 504
    spans = trace_service.get_spans_for_trace(trace_id, 3)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") == 504
    assert get_attr(outgoing, "nginx.downstream.status_code") is None
    assert get_attr(outgoing, "nginx.upstream.outcome") is None
    assert get_attr(outgoing, "nginx.upstream.generated_status_code") is None
    assert get_attr(outgoing, "nginx.upstream.failure.type") is None
    assert get_attr(outgoing, "nginx.upstream.failure.stage") is None
    assert get_attr(outgoing, "error.type") == "504"


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg51_retry_failure_then_success_keeps_diagnostics_only_on_failed_event(client, trace_service):
    trace_id, headers = sg51_trace_headers()
    assert sg51_raw_http_status("/sg4-retry", headers=headers) == 204
    spans = trace_service.get_spans_for_trace(trace_id, 3)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "nginx.downstream.status_code") is None
    assert get_attr(outgoing, "nginx.upstream.outcome") is None
    assert get_attr(outgoing, "nginx.upstream.failure.type") is None
    first, second = outgoing.events
    assert get_attr(first, "nginx.upstream.outcome") == "no_response"
    assert get_attr(first, "nginx.upstream.generated_status_code") == 502
    assert get_attr(first, "nginx.upstream.failure.type") == "connect_error"
    assert get_attr(first, "nginx.upstream.failure.stage") == "connect"
    assert get_attr(second, "nginx.upstream.outcome") is None
    assert get_attr(second, "http.response.status_code") == 204


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg51_error_page_separator_is_not_an_attempt_event(client, trace_service):
    trace_id, headers = sg51_trace_headers()
    assert sg51_raw_http_status("/sg51-error-page", headers=headers) == 204
    spans = trace_service.get_spans_for_trace(trace_id, 4)
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    assert get_attr(outgoing, "http.response.status_code") == 204
    assert get_attr(outgoing, "nginx.upstream.attempts") == 2
    assert len(outgoing.events) == 2
    assert [get_attr(e, "nginx.upstream.attempt") for e in outgoing.events] == [1, 2]
    assert all(get_attr(e, "nginx.upstream.peer") is not None for e in outgoing.events)
    assert [get_attr(e, "http.response.status_code") for e in outgoing.events] == [502, 204]


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_location_off_overrides_global_on(client, trace_service):
    assert client.get("http://127.0.0.1:18080/sg4-off").status_code == 204
    spans = trace_service.get_spans(2)
    assert all(s.kind == trace_pb2.Span.SPAN_KIND_SERVER for s in spans)


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_pass_request_headers_off_skips_client(client, trace_service):
    assert client.get("http://127.0.0.1:18080/sg4-noheaders").status_code == 204
    spans = trace_service.get_spans(2)
    assert all(s.kind == trace_pb2.Span.SPAN_KIND_SERVER for s in spans)


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_explicit_traceparent_override_skips_client(client, trace_service):
    assert client.get("http://127.0.0.1:18080/sg4-explicit-traceparent").status_code == 204
    spans = trace_service.get_spans(2)
    assert all(s.kind == trace_pb2.Span.SPAN_KIND_SERVER for s in spans)


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_access_rejection_does_not_create_client(client, trace_service):
    assert client.get("http://127.0.0.1:18080/sg4-access-deny").status_code == 403
    span = trace_service.get_span()
    assert span.kind == trace_pb2.Span.SPAN_KIND_SERVER


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_try_files_named_proxy_parenting(client, trace_service):
    assert client.get("http://127.0.0.1:18080/sg4-try-files").status_code == 204
    spans = trace_service.get_spans(3)
    server_spans = [s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_SERVER]
    outgoing = next(s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT)
    backend = next(s for s in server_spans if s.name == "/sg4-backend")
    frontend = next(s for s in server_spans if s.span_id == outgoing.parent_span_id)
    assert frontend.trace_id == outgoing.trace_id == backend.trace_id
    assert backend.parent_span_id == outgoing.span_id


def assert_sg4_native_proxy_response(r):
    assert r.status_code == 201
    assert r.text == "SG4-NATIVE-OK"
    assert r.headers["X-SG4-Native-Method"] == "GET"
    assert r.headers["X-SG4-Native-Arg"] == "native-query"
    assert r.headers["X-SG4-Native-Header"] == "native-header"


# The module is loaded, but the SG4 upstream CLIENT feature is left at its
# default off state. Native proxy response semantics must remain unchanged.
def test_sg4_default_off_preserves_native_proxy_semantics(client, trace_service):
    r = client.get(
        "http://127.0.0.1:18080/sg4-native-proxy?probe=native-query",
        headers={"X-SG4-Native": "native-header"},
    )
    assert_sg4_native_proxy_response(r)
    assert not r.headers.get("X-Otel-Upstream-Span-Id")
    spans = trace_service.get_spans(2)
    assert len(spans) == 2
    assert all(s.kind == trace_pb2.Span.SPAN_KIND_SERVER for s in spans)


@pytest.mark.parametrize(
    "nginx_config", [{"upstream_span": sg4_http_config()}], indirect=True
)
def test_sg4_enabled_preserves_native_proxy_semantics_and_emits_client(
    client, trace_service
):
    r = client.get(
        "http://127.0.0.1:18080/sg4-native-proxy?probe=native-query",
        headers={"X-SG4-Native": "native-header"},
    )
    assert_sg4_native_proxy_response(r)
    spans = trace_service.get_spans(3)
    client_spans = [s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_CLIENT]
    server_spans = [s for s in spans if s.kind == trace_pb2.Span.SPAN_KIND_SERVER]
    assert len(client_spans) == 1
    assert len(server_spans) == 2
    outgoing = client_spans[0]
    frontend = next(s for s in server_spans if s.name == "/sg4-native-proxy")
    backend = next(s for s in server_spans if s.name == "/sg4-native-backend")
    assert outgoing.parent_span_id == frontend.span_id
    assert backend.parent_span_id == outgoing.span_id
    assert r.headers["X-Otel-Upstream-Span-Id"] == outgoing.span_id.hex()
    assert len(r.headers["X-Otel-Upstream-Span-Id"]) == 16
    assert get_attr(outgoing, "http.response.status_code") == 201
    assert get_attr(outgoing, "url.path") == "/sg4-native-proxy"


@pytest.mark.parametrize(
    "nginx_config",
    [{"interval": "200ms", "endpoint": "http://127.0.0.1:14317"}],
    indirect=True,
)
@pytest.mark.parametrize("batch_count", [1, 3])
def test_batches(client, trace_service, batch_count):
    batch_size = 3

    for _ in range(
        batch_count * batch_size + 1
    ):  # +1 request to trigger batch sending
        assert client.get("http://127.0.0.1:18080/ok").status_code == 200

    time.sleep(0.01)

    assert len(trace_service.batches) == batch_count

    for batch in trace_service.batches:
        assert (
            get_attr(batch[0].resource, "service.name")
            == "unknown_service:nginx"
        )
        assert len(batch[0].scope_spans[0].spans) == batch_size

    time.sleep(0.3)  # wait for +1 request to be flushed
    trace_service.batches.clear()


@pytest.mark.parametrize(
    "nginx_config",
    [
        {
            "resource_attrs": """
                otel_service_name "test_service";
                otel_resource_attr my.name "my name";
                otel_resource_attr my.service "my service";
            """,
        }
    ],
    indirect=True,
)
def test_custom_resource_attributes(client, trace_service):
    assert client.get("http://127.0.0.1:18080/ok").status_code == 200

    batch = trace_service.get_batch()

    assert get_attr(batch.resource, "service.name") == "test_service"
    assert get_attr(batch.resource, "my.name") == "my name"
    assert get_attr(batch.resource, "my.service") == "my service"


@pytest.mark.parametrize(
    "nginx_config",
    [
        {
            "exporter_opts": """
                header X-API-TOKEN api.value;
                header Authorization "Basic value";
            """,
        }
    ],
    indirect=True,
)
@pytest.mark.parametrize("trace_service", ["skip_otelcol"], indirect=True)
def test_exporter_headers(client, trace_service):
    assert client.get("http://127.0.0.1:18080/ok").status_code == 200

    assert trace_service.get_span().name == "/ok"

    headers = dict(trace_service.last_metadata)
    assert headers["x-api-token"] == "api.value"
    assert headers["authorization"] == "Basic value"


@pytest.mark.parametrize(
    "nginx_config",
    [
        {
            "endpoint": "https://localhost:14318",
            "exporter_opts": "trusted_certificate localhost.crt;",
        }
    ],
    indirect=True,
)
def test_tls_export(client, trace_service):
    assert client.get("http://127.0.0.1:18080/ok").status_code == 200

    assert trace_service.get_span().name == "/ok"
