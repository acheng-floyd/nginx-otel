# SG5.1 downstream modifications

Release: `v0.1.2-pr97-sg5.1`
Parent release: `v0.1.2-pr97-sg5`
Audited SG5 parent commit from GitHub Actions: `2d35803af27cb458584e0d3278a46808fd5169cd`

SG5.1 is a correctness/performance follow-up to SG5. It preserves the SG4.17/SG5
one-wire CLIENT span architecture and does not patch NGINX/OpenResty core.

## Runtime changes

1. **Failure-only SG5 diagnostics on the success path**
   - A real upstream HTTP response continues to use `http.response.status_code`.
   - SG5.1 no longer writes `nginx.upstream.outcome=http_response` on successful
     CLIENT spans/events.
   - `nginx.downstream.status_code` is emitted only when the final real upstream
     attempt produced no HTTP response.
   - This removes the three SG5 classification KeyValues that were added to the
     common one-attempt success path.

2. **Truthful coarse failure stage**
   - Removes `nginx.upstream.failure.phase=response_header`.
   - Adds `nginx.upstream.failure.stage=connect|after_connect`.
   - `connect_time != -1` proves that NGINX reached the upstream send path, but
     cannot distinguish request-send timeout from response-read timeout after the
     fact. SG5.1 deliberately does not claim more precision than retained public
     upstream state can prove.

3. **Filter NGINX upstream-state separators**
   - `peer == NULL` entries are treated as NGINX's separator between distinct
     upstream operations, not as attempts.
   - `nginx.upstream.attempts`, attempt Event numbering, and the 16-event cap now
     operate on real `peer != NULL` attempts only.
   - The final upstream status/failure classification scans backward to the final
     real attempt rather than blindly using the final raw array element.

## Test/CI changes

- Replaces the SG5 malformed-status-line fault with a deterministic valid status
  line plus malformed header field.
- Fault cases use a small stdlib HTTP/1.1 client plus unique W3C trace IDs so a
  failed client assertion cannot poison later trace-count assertions.
- Adds an upstream request-send timeout test: buffered 16 MiB POST to a peer that
  accepts TCP but does not read, with `proxy_send_timeout 100ms`.
- Adds a vanilla NGINX `error_page -> second proxy` separator regression.
- Tightens the existing OpenResty error-page qualification to require exactly two
  real attempt events, not a filtered view over a phantom separator event.
- Vanilla suite target: 50 tests, 0 failure/error/skip.
- OpenResty dedicated qualification: 6 tests, 0 failure/error/skip.
- OpenResty complete suite target: 56 tests, 0 failure/error/skip.
- GitHub checkout actions move from `actions/checkout@v4` to `@v5` to avoid the
  Node 20 deprecation warning seen in the SG5 Actions log.

## Preserved security boundaries

SG5.1 does not add peer/session callback wrappers, does not mutate the NGINX phase
handler array, does not retain request-pool pointers in exporter background
threads, and keeps SG4 protobuf Event reuse cleanup and SG4.9 synthetic status
truthfulness.
