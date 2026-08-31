# License and redistribution compliance

This downstream source kit is distributed under the **Apache License, Version 2.0**
(`Apache-2.0`), matching the license of the upstream `nginx/nginx-otel` project.

Upstream project:

- Project: `nginx/nginx-otel`
- Reviewed baseline: `v0.1.2`
- Reviewed baseline commit: `72d8eed53af4c2cd6f3e30a2efe0e38d66f5e176`
- Upstream license: Apache License 2.0
- Upstream copyright/trademark notices remain the property of their respective owners.

## What the generated repository does

`create-custom-repo.sh` starts from the exact upstream `nginx-otel` v0.1.2 commit.
The upstream `LICENSE` file is retained **unchanged**. SG4 downstream changes are
then applied and committed on top.

The generated repository also receives:

- `MODIFICATIONS-SG4.md` -- a prominent summary of downstream modifications.
- `THIRD_PARTY_NOTICES.md` -- provenance and reference-project licensing notes.
- `LICENSE-COMPLIANCE.md` -- this redistribution guidance.
- Per-file modification notices in every retained upstream source/test/CI file changed by SG4/SG4.5/SG4.6/SG4.7. The SG4.5 removal of `.github/dependabot.yml` is recorded in `MODIFICATIONS-SG4.md`.

These measures are intended to make the Apache-2.0 redistribution obligations
easy to audit, especially the requirements to keep the license, preserve notices,
and mark modified files.

## Redistribution checklist

When publishing this fork or distributing its source/binaries:

1. Keep the upstream `LICENSE` file with the distribution.
2. Do not remove upstream copyright, patent, attribution, or trademark notices. If the exact upstream revision contains a `NOTICE` file, retain and redistribute the applicable NOTICE content as required by Apache-2.0.
3. Keep the SG4 modification notices in retained modified files and keep `MODIFICATIONS-SG4.md` so deleted/replaced policy files remain documented.
4. Keep `MODIFICATIONS-SG4.md` with source distributions.
5. If you later copy or vendor third-party source code, re-check that project's
   license and NOTICE/attribution requirements; the current SG4 kit does not
   vendor APISIX, Kong, or OpenTelemetry cpp-contrib source files.
6. Do not imply that this downstream fork is an official F5/NGINX release or that
   use of the Apache-2.0 license grants rights to NGINX/F5 trademarks.

## Binary distribution

Apache-2.0 permits redistribution in source or object form subject to its terms.
For an OpenResty/nginx module binary distribution, ship a readable copy of the
Apache-2.0 license with the binary package or accompanying documentation and keep
the applicable attribution/modification information.

## License choice for this fork

The project intentionally remains Apache-2.0 rather than introducing a different
top-level license. This keeps the licensing model aligned with upstream and avoids
unnecessary ambiguity around the modified upstream files.

This file is engineering-oriented compliance guidance, not legal advice.
