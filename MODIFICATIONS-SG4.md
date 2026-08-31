# SG4 downstream modifications

This repository is a downstream modification of `nginx/nginx-otel` v0.1.2,
licensed under the Apache License, Version 2.0.

Reviewed upstream baseline:

```text
72d8eed53af4c2cd6f3e30a2efe0e38d66f5e176
```

SG4/SG4.1/SG4.2/SG4.3/SG4.4/SG4.5/SG4.6/SG4.7/SG4.8/SG4.9 modifies the following upstream files when the custom repository is
generated:

```text
src/ngx.hpp
src/batch_exporter.hpp
src/http_module.cpp
tests/test_otel.py
tests/trace_service.py
.github/workflows/ubuntu.yml
.github/dependabot.yml  # removed by generated SG4.5+ tree; SG4.7 preserves the reviewed intermediate management commit only in Git history
```

Each retained modified generated file contains a prominent downstream-modification notice; deleted policy files are documented here instead.

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

## SG4.5 compile/CI follow-up

`v0.1.2-pr97-sg4.5` keeps the SG4 CLIENT-span runtime model and adds only the following compile/CI policy fixes:

- `src/http_module.cpp`: legacy SERVER `http.status_code` now casts `ngx_uint_t` to `int` explicitly to avoid ambiguity after SG4 added an `int64_t` BatchExporter attribute overload. HTTP status values are safely within `int`.
- `.github/workflows/ubuntu.yml`: CI checks out `nginx/nginx` at `release-1.31.1`, matching the audited ABI/version guard.
- `.github/dependabot.yml`: removed. The upstream file only enabled weekly GitHub Actions version-update PRs; this audited downstream fork uses deliberate/manual CI dependency upgrades instead.

These changes do not alter the SG4 CLIENT-span runtime model.


## SG4.6 publication-commit follow-up

`v0.1.2-pr97-sg4.6` does not change the generated SG4.5 source/test/CI tree. It changes only the repository-construction publication mechanism for an existing SG4.4 destination:

- freezes the fully staged/audited candidate source with `git write-tree`;
- creates the release commit with `git commit-tree` and an explicitly verified SG4.4 parent;
- verifies the new commit tree equals the frozen candidate tree and that the parent is exact;
- moves the local branch to that commit and requires a clean repository before tag/pre-push.

This replaces the SG4.5 `git reset --soft` re-parent path and prevents a staged-only intermediate tree from being treated as a publishable repository.


## SG4.7 reviewed destination-history follow-up

`v0.1.2-pr97-sg4.7` does not change the generated SG4.6 runtime/source/test/CI tree. It changes only how an already-published downstream history is accepted as a parent.

The actual downstream `main` contains one separately reviewed repository-management commit after SG4.4:

```text
3a9fa6a4e2f841f3fdb8d4d8df4c5175c9182e40
Update Dependabot configuration for GitHub Actions
```

That commit is a direct child of the SG4.4 release commit and changes only `.github/dependabot.yml`. SG4.7 pins the exact commit SHA and re-verifies the direct-parent relationship, one-commit range, exact `M .github/dependabot.yml` path/status, and absence of runtime/test/build changes. Any different post-SG4.4 history is rejected pending a new review.

The final SG4.7 tree still removes `.github/dependabot.yml` according to the SG4.5 policy. The reviewed intermediate commit is preserved only as immutable Git history, and the SG4.7 release commit is a normal fast-forward child of the actual destination `main`.

## SG4.8 native-safety/test follow-up

`v0.1.2-pr97-sg4.8` keeps the SG4.7 runtime tracing implementation unchanged. It repairs the invalid named-location pytest fixture, adds explicit native-proxy non-interference and module-active tests, and adds a CI gate that fails if tracked NGINX core source files are modified.

The generated repository remains an nginx-otel module fork. It does not vendor or patch NGINX core. CI checks out NGINX 1.31.1 separately and builds the dynamic module against that checkout.


## SG4.9 synthetic upstream status correction

`v0.1.2-pr97-sg4.9` changes the SG4 telemetry interpretation only. Real NGINX 1.31.1 CI demonstrated that `ngx_http_upstream_next()` can store NGINX-generated 502/504 values in `ngx_http_upstream_state_t.status` for transport failures before an upstream HTTP response exists.

SG4.9 therefore emits CLIENT/Event `http.response.status_code` only when `status != 0` and `header_time != (ngx_msec_t)-1`. The latter is the public upstream-state evidence that response-header processing completed. This keeps real backend HTTP 4xx/5xx statuses while suppressing synthetic transport-error statuses. No NGINX core source or request/upstream control-flow code is patched.
