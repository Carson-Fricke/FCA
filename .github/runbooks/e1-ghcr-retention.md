# E1 GHCR Image Retention

This runbook covers the scheduled prune of the private GHCR container image
(`ghcr.io/carson-fricke/fca`) published by `publish-image.yml` and consumed by
`deploy-main.yml`. It is an automatic cost-control loop with a manual
dispatch path; it never deletes the image the deployment currently relies on.

## Retention Rule

`.github/workflows/prune-ghcr.yml` runs weekly (Sundays 03:00 UTC) and on
manual `workflow_dispatch`, and deletes image versions that meet **all** of:

- outside the newest 10 versions (versions are ranked newest-first by
  `created_at`);
- older than 7 days (strictly: `created_at < now - 7d`);
- not protected by the moving-tag safety net (see below).

Everything else is retained. Storage growth is therefore bounded at roughly
`max(10, 7 days of deploys) x image size` at the current deploy cadence.

## What Is Never Deleted (Deployed-Digest Safety)

The Azure runtime does not pull from GHCR: `deploy-main.yml` imports the
digest-pinned image into a private ACR (`az acr import --source
ghcr.io/...@<digest>`) and workload containers reference the ACR copy, so
deleting a GHCR version cannot break a running Container App.

GHCR remains the re-import source for rollback: re-running `deploy-main.yml`
on an older commit imports `ghcr.io/carson-fricke/fca@<digest>` again. The
retention rule protects that reference:

- A digest deployed within the last 7 days is younger than the age floor and
  can never be deleted, regardless of how many newer deploys exist. At the
  observed cadence (31 deploys in 4 days) the newest-10 window covers only
  ~1.3 days, so the 7-day floor is the operative protection.
- A digest deployed more than 7 days ago is retained only while it sits in
  the newest 10. Rolling back past the 7-day window may therefore require
  rebuilding the image from that commit (`publish-image.yml` on the old SHA)
  rather than re-importing from GHCR. This is beyond the documented rollback
  window and is accepted.
- GHCR versions are not rebuildable: deleting a version removes the manifest
  and its commit-SHA tag, and the digest cannot be reproduced by rebuilding.
  This is why the age floor is 7 days (T212 AC2), not shorter.
- Moving-tag safety net: any version carrying a tag that is not a 40-hex
  commit SHA is never deleted. The publish workflow tags only commit SHAs, so
  this set is empty today; it exists so a future mutable tag cannot be
  orphaned by a prune.

## Run Manually

1. Open the repository's Actions tab, select **Prune GHCR image versions**,
   and click **Run workflow** (`workflow_dispatch`), or use:

   ```powershell
   gh workflow run prune-ghcr.yml
   gh run watch --workflow prune-ghcr.yml
   ```

2. The first manual run is the real test: the dry-run reasoning here and the
   YAML checks cannot exercise live GHCR deletions because they require the
   repository token. Watch the run log for the survey and deletion summary.

3. The log must report the surveyed version count and the deletion count,
   ending in a notice like
   `GHCR prune complete: surveyed N version(s); deleted D/N; ...`.
   Per-version delete failures are warnings, not failures; the next weekly
   run retries them.

## Inspect GHCR Versions

```powershell
# List package versions (newest first) with id, created_at, and tags
gh api --paginate --header "Accept: application/vnd.github+json" `
  "/user/packages/container/fca/versions?per_page=100" `
  --jq '.[] | [.id, .created_at, (.metadata.container.tags | join(","))] | @tsv'
```

The same listing is what the workflow surveys: it pages through versions,
keeps the newest 10 plus anything under the age floor, and DELETEs the rest
by version id. Package-level settings (visibility, role access) live at
`https://github.com/users/Carson-Fricke/packages/container/fca/settings`.

## Safety Limits

- The workflow runs with `permissions: packages: write` and no other
  write scope; it has no Azure access and cannot touch the ACR mirror.
- The survey aborts with an error above 30 pages (~3000 versions) instead of
  guessing; it never deletes from a truncated view.
- `concurrency: prune-ghcr` serializes runs; overlapping dispatches queue
  rather than racing the delete loop.
- Do not lower the 7-day floor to shrink the bill: the floor, not the
  newest-10 window, is what guarantees rollback re-imports within the Azure
  rollback window. Image slimming is the separate cost lever.
