# SG5.1 failure-classification design

Parent: `v0.1.2-pr97-sg5`
Target: NGINX 1.31.1 / OpenResty 1.31.1.1

## Design goal

Keep standard HTTP semantics unchanged while making transport/upstream failures
queryable without fabricating a backend HTTP response or an exact errno that is
not retained in `ngx_http_upstream_state_t` at LOG phase.

## Normal successful HTTP response

No SG5.1-specific classification fields are necessary:

```text
http.response.status_code = 200/204/...
nginx.upstream.attempts   = N
```

For a normal one-attempt success, SG5.1 therefore adds zero failure-classification
KeyValues compared with the inherited SG4.17 data model.

## Final attempt has no upstream HTTP response

```text
http.response.status_code             absent
nginx.downstream.status_code          <final NGINX response when available>
nginx.upstream.outcome                no_response
nginx.upstream.generated_status_code  502 or 504 when NGINX recorded one
nginx.upstream.failure.type           connect_error | response_error | timeout
nginx.upstream.failure.stage          connect | after_connect
error.type                            nginx.upstream.no_response
```

`after_connect` intentionally covers both request-send and response-read failures.
NGINX 1.31.1 records `connect_time` before/while entering the request-send path;
therefore retained LOG-phase state cannot prove that an after-connect timeout was
specifically a response-header timeout.

## Real HTTP 5xx from the backend

```text
http.response.status_code = 502/504/...
error.type                = "502"/"504"/...
```

No `generated_status_code`, failure type/stage, downstream duplicate, or
`outcome=http_response` is emitted.

## Retry

A failed transport attempt carries failure diagnostics on that attempt Event.
A later real HTTP success carries its normal HTTP status. The logical CLIENT span
reflects the final real attempt and does not inherit stale failure diagnostics
from an earlier retry.

## Upstream-state separators

NGINX may retain a zeroed `peer == NULL` element between separate upstream
operations on one main request (for example first proxy -> `error_page` -> second
proxy). SG5.1 mirrors NGINX's own `$upstream_*` rendering semantics by excluding
that separator from attempt counts and Events.
