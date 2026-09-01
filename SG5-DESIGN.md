# SG5 upstream failure classification design

Date: 2026-09-01
Parent: `v0.1.2-pr97-sg4.17`
Target: NGINX 1.31.1 / OpenResty 1.31.1.1

## Goal

SG5 keeps the SG4.17 one-wire CLIENT span, retry Span Events, HTTP-status truthfulness,
protobuf reuse cleanup, content-entry wrapper, and all existing fail-open/guard behavior.
It adds a conservative telemetry layer that distinguishes:

- a real upstream HTTP response;
- a NGINX-generated 502/504 when no upstream HTTP response was received;
- timeout before connect completed versus timeout while waiting for response headers;
- generic connect-side failure versus generic response-header-side failure.

SG5 does **not** change the HTTP status NGINX returns to the client and does not introduce
private 59x status codes.

## Why classification is deliberately coarse

`ngx_http_upstream_state_t` retains peer, status, timings, response length and byte counters,
but it does not retain the original `errno` or the transient `NGX_HTTP_UPSTREAM_FT_*` reason
that was passed through the upstream failure path. Therefore a post-request telemetry module
cannot truthfully distinguish every `ECONNREFUSED`, `ECONNRESET`, invalid header, premature
close, TLS failure, or no-live-upstream case from public state alone.

SG5 consequently uses only evidence that survives in the audited public/upstream state:

- `header_time != (ngx_msec_t)-1` proves a real upstream response header was processed;
- `connect_time != (ngx_msec_t)-1` proves the connection phase completed;
- a synthetic state status 504 without a real header is classified as `timeout`;
- a synthetic state status 502 without a real header is classified as either
  `connect_error` or `response_error` according to whether connect completed.

This avoids telemetry labels that claim more precision than NGINX state can prove.

## New CLIENT / attempt Event attributes

```text
nginx.downstream.status_code
nginx.upstream.outcome
nginx.upstream.generated_status_code
nginx.upstream.failure.type
nginx.upstream.failure.phase
```

Semantics:

| Attribute | Meaning |
|---|---|
| `nginx.downstream.status_code` | Final HTTP status NGINX returned to the downstream client |
| `nginx.upstream.outcome=http_response` | Final/attempt upstream produced a real HTTP response header |
| `nginx.upstream.outcome=no_response` | No real upstream HTTP response header was observed |
| `nginx.upstream.generated_status_code` | NGINX synthetic upstream-state 502/504 when no real HTTP response was observed |
| `nginx.upstream.failure.type=timeout` | NGINX synthetic 504 without a real upstream response |
| `nginx.upstream.failure.type=connect_error` | NGINX synthetic 502 before connect completed; intentionally not labeled `connect_refused` |
| `nginx.upstream.failure.type=response_error` | NGINX synthetic 502 after connect completed but before a valid response header |
| `nginx.upstream.failure.phase=connect` | Failure happened before successful connect completion |
| `nginx.upstream.failure.phase=response_header` | TCP/upstream connect completed; failure occurred before a valid HTTP response header |

The existing SG4 field remains unchanged:

```text
error.type=nginx.upstream.no_response
```

for a final no-response 5xx. Existing dashboards therefore do not have to migrate just to
consume SG5.

## Examples

### Connect refused / generic connect error

```text
http.response.status_code          absent
nginx.downstream.status_code       502
nginx.upstream.outcome             no_response
nginx.upstream.generated_status_code 502
nginx.upstream.failure.type        connect_error
nginx.upstream.failure.phase       connect
error.type                         nginx.upstream.no_response
```

SG5 calls this `connect_error`, not `connect_refused`, because the retained state does not
prove the original errno.

### Connect timeout

```text
http.response.status_code          absent
nginx.downstream.status_code       504
nginx.upstream.generated_status_code 504
nginx.upstream.failure.type        timeout
nginx.upstream.failure.phase       connect
```

### Response-header/read timeout

```text
http.response.status_code          absent
nginx.downstream.status_code       504
nginx.upstream.generated_status_code 504
nginx.upstream.failure.type        timeout
nginx.upstream.failure.phase       response_header
```

### Reset/premature close/invalid header after connect

```text
http.response.status_code          absent
nginx.downstream.status_code       502
nginx.upstream.generated_status_code 502
nginx.upstream.failure.type        response_error
nginx.upstream.failure.phase       response_header
```

The classification is intentionally coarse; the test harness exercises reset, malformed
header and partial-header-close separately to make sure all remain truthful `response_error`
rather than being falsely given exact errno labels.

### Real backend HTTP 504

```text
http.response.status_code          504
nginx.downstream.status_code       504
nginx.upstream.outcome             http_response
nginx.upstream.generated_status_code absent
nginx.upstream.failure.type        absent
error.type                         504
```

This is the key distinction from an NGINX-generated timeout 504.

## Retry behavior

SG5 preserves the SG4 one-wire model. The logical CLIENT span reflects the final attempt;
each retry event carries its own outcome and coarse failure classification. Example:

```text
CLIENT
  nginx.downstream.status_code=204
  nginx.upstream.outcome=http_response

  event #1
    nginx.upstream.outcome=no_response
    nginx.upstream.generated_status_code=502
    nginx.upstream.failure.type=connect_error
    nginx.upstream.failure.phase=connect

  event #2
    nginx.upstream.outcome=http_response
    http.response.status_code=204
```

## Explicit non-goals for SG5

- no NGINX/OpenResty core patch;
- no `peer.data`, `peer.get/free`, `peer.set_session`, or `peer.save_session` wrapping;
- no custom 598/599 HTTP response codes;
- no exact errno classification from unavailable state;
- no change to SG4.17 `otel_upstream_span` configuration or CLIENT context propagation;
- no change to SG4 protobuf event-reuse cleanup;
- no expansion to grpc/fastcgi/uwsgi/scgi/Lua cosocket instrumentation.

If exact `ECONNREFUSED` / `ECONNRESET` / TLS-handshake / DNS-resolution reasons become a
requirement later, that should be a separately audited design because it requires capturing
failure information earlier in the NGINX upstream control path.

## SG5 CI qualification matrix

SG5 keeps all SG4.17 tests and adds eight general failure-classification cases:

1. immediate dead-port/connect failure -> coarse `connect_error`, generated 502;
2. deterministic connect timeout using a CI-local iptables DROP -> `timeout/connect`, 504;
3. accepted TCP connection that stalls before headers -> `timeout/response_header`, 504;
4. TCP reset before response header -> `response_error/response_header`, 502;
5. malformed HTTP response header -> `response_error/response_header`, 502;
6. partial header then close -> `response_error/response_header`, 502;
7. real backend HTTP 504 -> real `http.response.status_code=504`, no generated/failure type;
8. connect failure followed by successful retry -> failure classification stays on attempt #1,
   final CLIENT reports successful HTTP response.

Expected GitHub Actions gates:

```text
Vanilla NGINX 1.31.1:       48/48, 0 skip/failure/error
OpenResty SG4 qualification: 6/6, 0 skip/failure/error
OpenResty complete suite:    54/54, 0 skip/failure/error
sg5-dual-runtime-gate:       PASS
```

The source kit can statically validate the transformer and fixtures, but SG5 is not qualified
until the real GitHub Actions runs above pass.
