# SG4 downstream modifications

This repository is a downstream modification of `nginx/nginx-otel` v0.1.2,
licensed under the Apache License, Version 2.0.

Reviewed upstream baseline:

```text
72d8eed53af4c2cd6f3e30a2efe0e38d66f5e176
```

SG4/SG4.1/SG4.2/SG4.3/SG4.4 modifies the following upstream files when the custom repository is
generated:

```text
src/ngx.hpp
src/batch_exporter.hpp
src/http_module.cpp
tests/test_otel.py
tests/trace_service.py
```

Each of those generated files contains a prominent downstream-modification notice.

## Functional modifications

The SG4 runtime implementation:

- Adds an inherited `otel_upstream_span` switch for `proxy_pass` instrumentation.
- Creates one real outgoing HTTP CLIENT span/context for the logical upstream
  operation and preserves the span ID actually propagated on the wire.
- Records NGINX retry attempts as `nginx.upstream.attempt` Span Events using
  `r->upstream_states`, instead of fabricating per-attempt CLIENT span IDs.
- Uses stable/newer HTTP semantic attribute names for the custom CLIENT span and
  intentionally avoids legacy dual-write.
- Keeps the CLIENT URL query-free (`url.path`) to reduce secret/high-cardinality
  exposure.
- Adds protobuf Event-reuse cleanup to avoid cross-request telemetry residue.
- Avoids PR #97 peer/session callback wrapping.
- Hard-pins the audited NGINX/OpenResty ABI target.

## SG4.1 packaging revision

`v0.1.2-pr97-sg4.1` is a licensing/compliance packaging refresh of SG4. The
runtime tracing design is unchanged from SG4. The revision adds:

- Full Apache-2.0 license in the source kit.
- Explicit license/redistribution documentation.
- Third-party reference/provenance notes.
- Per-file modification notices in generated modified upstream files.
- Static gates that verify the upstream LICENSE is retained unchanged.

See `CHANGELOG-CUSTOM.md` and `audit/SG4.1_LICENSE_COMPLIANCE_AUDIT.md` for details.


## SG4.2 source-kit revision

`v0.1.2-pr97-sg4.2` does not change the SG4 runtime tracing model. It fixes the
repository-construction tooling used on a pristine official v0.1.2 checkout:

- replaces the brittle multi-line `onRequestStart()` tail insertion anchor with
  the unique `getServerName()` function anchor in the exact pinned baseline;
- rejects malformed mixed Git remotes such as `git@https://github.com/...`;
- keeps the SG4.1 Apache-2.0 compliance material and generated-file modification
  notices unchanged.

See `CHANGELOG-CUSTOM.md` and `audit/SG4.2_SCRIPT_FIX_AUDIT.md`.

## SG4.3 source-kit revision

`v0.1.2-pr97-sg4.3` does not change the SG4 runtime tracing model. It hardens
the repository-construction path after real Builder runs exposed a second
format-sensitive source anchor. The transformer replaces `onRequestEnd()` by
parsed C/C++ function boundaries. Before touching the real checkout, preflight
runs the same complete transformer and source verifier on a disposable five-file
copy. The construction path also removes inherited official-branch tracking,
normalizes Git checkout behavior, verifies staging/tag/remotes, and adds a
non-force pre-push history/ref guard.

See `audit/SG4.3_CONSTRUCTION_AUDIT.md`.

## SG4.4 source-kit revision

`v0.1.2-pr97-sg4.4` does not change the SG4 runtime tracing model or the SG4
modifications applied to the five upstream source/test files. It fixes a
publication-side effect found on a real Rocky 10 Builder: the SG4.3 verifier
used `python3 -m py_compile`, which created `tests/__pycache__/*.pyc`; the strict
staging guard then correctly refused an incomplete/untracked commit. SG4.4 uses
in-memory `compile()` syntax checks, makes preflight assert that verification is
bytecode/cache-free, and defensively removes only test bytecode immediately
before the strict staging gate. It also avoids rewriting an already-existing
annotated remote tag when that tag peels to the same generated commit.

See `audit/SG4.4_PUBLICATION_FIX_AUDIT.md`.
