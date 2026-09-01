# SG5 downstream modifications

SG5 is a downstream extension of the audited `v0.1.2-pr97-sg4.17` release. The complete SG4
history and inherited runtime behavior remain documented in `MODIFICATIONS-SG4.md`.

## Runtime delta from SG4.17

SG5 changes `src/http_module.cpp` telemetry materialization only:

- adds `nginx.downstream.status_code` to the custom upstream CLIENT span;
- adds `nginx.upstream.outcome=http_response|no_response`;
- records `nginx.upstream.generated_status_code` only for NGINX synthetic upstream-state
  status when no real upstream HTTP header was observed;
- adds conservative `nginx.upstream.failure.type=timeout|connect_error|response_error`;
- adds `nginx.upstream.failure.phase=connect|response_header`;
- adds the same outcome/failure attributes to every recorded `nginx.upstream.attempt` Event.

SG5 intentionally keeps the existing final no-response contract
`error.type=nginx.upstream.no_response` so SG4 dashboards remain compatible.

## Runtime behavior inherited unchanged

- one real wire CLIENT span id per logical proxy operation;
- retries represented as bounded Span Events;
- SG4.9 real-vs-synthetic HTTP status truthfulness (`header_time` gate);
- protobuf Span/Event reuse cleanup;
- query-free CLIENT `url.path` and no legacy CLIENT HTTP dual-write;
- SG4.16 proxy content-entry CLIENT preparation and `NGX_HTTP_VAR_NOCACHEABLE` variable fix;
- all existing fail-open, ambiguity, subrequest, explicit-header and request-header guards;
- no peer/session callback wrapping;
- NGINX 1.31.1 / OpenResty 1.31.1.1 ABI pin.

## Test / CI delta

`tests/test_otel.py` gains eight SG5 cases for connect failure, connect timeout, response-header
timeout, reset, malformed header, partial header, real 504 and retry recovery. The connect
-timeout case is made deterministic by a CI-local `iptables OUTPUT DROP` rule on port 18087.

The dual-runtime workflow now requires:

- vanilla NGINX: 48/48;
- OpenResty-specific: 6/6;
- OpenResty complete: 54/54;
- final `sg5-dual-runtime-gate` success.

## Files additionally distributed by the source kit

- `MODIFICATIONS-SG5.md`
- `SG5-DESIGN.md`
- `audit/SG5_FAILURE_CLASSIFICATION_AUDIT.md`

SG5 remains Apache-2.0 and does not modify the upstream LICENSE.
