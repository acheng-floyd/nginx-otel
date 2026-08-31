# Third-party notices and design references

## nginx/nginx-otel

The generated downstream repository is based on:

- `nginx/nginx-otel` v0.1.2
- Commit `72d8eed53af4c2cd6f3e30a2efe0e38d66f5e176`
- License: Apache License, Version 2.0

The upstream `LICENSE` is retained unchanged in the generated repository.
Upstream copyright and trademark notices are not replaced by this downstream kit.

PR #97 was reviewed as provenance/design input. SG4 does not wholesale
cherry-pick the PR and deliberately does not reintroduce its peer/session callback
wrapping.

## Reference projects

The SG4 design review also studied public implementations from:

- Apache APISIX -- Apache License 2.0
- Kong Gateway -- Apache License 2.0
- OpenTelemetry C++ Contrib (`instrumentation/nginx`) -- Apache License 2.0

These projects were used as architectural/reference material for concepts such as
late span materialization, balancer/retry tracing, propagation, and NGINX
instrumentation structure.

**No APISIX, Kong, or OpenTelemetry cpp-contrib source file is vendored or
wholesale copied into this source kit.** Their repositories are therefore listed
for provenance and engineering traceability rather than as bundled components.

If future revisions copy or vendor code from any reference project, re-evaluate
that project's current LICENSE and any NOTICE/attribution requirements at that
revision before redistribution.

Reference URLs:

- https://github.com/nginx/nginx-otel
- https://github.com/apache/apisix
- https://github.com/Kong/kong
- https://github.com/open-telemetry/opentelemetry-cpp-contrib

## OpenResty qualification source

SG4.10-SG4.13 CI downloads OpenResty 1.31.1.1 only as a build/test dependency for the
qualification job.  The OpenResty source is not vendored or redistributed in
this source kit; its own licenses and notices remain applicable to that source
release.
