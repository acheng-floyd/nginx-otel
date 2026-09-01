# SG5.1 security, correctness and performance re-audit

## Inputs

- Published SG5 source supplied by the user.
- SG5 GitHub Actions run `logs_90664885132.zip`.
- SG5 Actions checkout commit: `2d35803af27cb458584e0d3278a46808fd5169cd`.
- Exact runtime targets remain NGINX 1.31.1 / OpenResty 1.31.1.1.

## SG5 Actions evidence

- OpenResty-specific SG4 compatibility qualification: 6/6 PASS.
- Vanilla SG5 suite reached 32 PASS before the malformed-header case timed out in
  the multiplexed Python client.
- OpenResty complete suite later showed the same malformed-header client timeout;
  subsequent span-count failures were consistent with trace-buffer contamination
  after the first test aborted before collection.
- Final dual-runtime gate correctly failed closed.

## Security review

No SG5.1 change introduces peer callback wrapping, session callback replacement,
worker-global phase-array mutation, background access to request-pool pointers,
or unbounded retry Event growth. No new clear Critical/High RCE, UAF, double-free,
or OOB path was identified by this static re-audit.

## Correctness fixes

- `failure.phase=response_header` was over-precise because `connect_time` cannot
  distinguish send timeout from read-header timeout. SG5.1 uses `stage=after_connect`.
- `peer == NULL` upstream-state separators are excluded from final-state selection,
  attempt count and Event materialization.
- Fault tests use unique traces and deterministic malformed-header bytes.

## Performance review

SG5 wrote classification fields on every successful request. SG5.1 removes those
redundant success-path fields. Failure-only diagnostics remain bounded and low
cardinality. Existing retry Events remain capped at 16 real attempts.

## Release status

Static/source-kit candidate only until real GitHub Actions reports:

```text
Vanilla NGINX 1.31.1: 50/50
OpenResty qualification: 6/6
OpenResty complete: 56/56
final SG5.1 dual-runtime gate: PASS
```

Production use should still include ASan/UBSan qualification and canary load
comparison against the frozen SG4.17 production candidate.
