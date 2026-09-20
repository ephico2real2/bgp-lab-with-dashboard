# CI evidence

This page is filled in by `ci/mkdoc.sh` after a green `lab-ci` run. Until then
there is nothing to cite — no invented check table, no invented timings.

<!-- mkdoc:placeholder -->
Bring the lab up with:

```bash
docker compose -f compose/docker-compose.yml up -d --wait
# or: ci/up.sh
```

Dashboard: http://127.0.0.1:8088

After a green run the workflow prints the exact line to render this file, with
the run URL, commit, image digest and screenshot base filled in:

```
ci/mkdoc.sh --run-url URL --commit SHA --digest DIGEST \
  --check-file ci/out/check.txt --timings-file ci/out/walk.log \
  --shot-base RAW_URL_BASE --out docs/CI-EVIDENCE.md
```

CI does not commit this file. A workflow that writes docs back to its own
branch is a loop nobody wants.
<!-- /mkdoc:placeholder -->
