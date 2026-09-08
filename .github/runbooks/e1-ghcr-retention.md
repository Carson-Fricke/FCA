# E1 GHCR Image Retention

This runbook covers the scheduled prune of the private GHCR container image
(`ghcr.io/carson-fricke/fca`) published by `publish-image.yml` and consumed by
`deploy-main.yml`. It is an automatic cost-control loop with a manual
dispatch path; it never deletes the image the deployment currently relies on.
It also documents the retention of the private ACR mirror that
`deploy-main.yml` imports each published digest into (per-service
keep-latest-and-previous, T208).

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

## ACR Mirror Retention (Per Service)

`deploy-main.yml` imports every published digest into the private ACR
(repository = lowercased GitHub repository name, e.g. `fca`, tagged with the
commit SHA) and deploys Container Apps by ACR digest. Unlike GHCR, that ACR is
where the runtime actually pulls, so ACR retention must stay ahead of cost
without ever stranding a running or rollback-able revision. Two mechanisms
cooperate:

1. **Registry-level retention policy** (`infra/modules/container-registry.bicep`)
   declares `properties.policies.retention` with `days: retentionDays`
   (default 7, range 7-90) and `status: retentionStatus` (default `enabled`),
   so the policy exists from Bicep with no portal change. Its limits:
   - It is a **Premium SKU** feature; the registry is deployed on Basic, so
     the declaration is inert until the SKU is upgraded.
   - The service purges only **untagged** manifests after the age threshold.
     The mirrored manifests keep their commit-SHA tag, so the policy never
     cleans the imported digests; it covers future untagged leftovers only.
2. **Per-service prune workflow** (`.github/workflows/prune-acr.yml`) is the
   enforced path. It runs weekly (Sundays 04:00 UTC), on manual
   `workflow_dispatch`, and is called by `deploy-main.yml` after a successful
   deploy (`acr-retention` job) so the just-deployed digest is always treated
   as retained regardless of survey ordering.

### Retention Rule

The workflow signs in with the deployer OIDC identity (no client secrets),
discovers the registry by the same tag filter as the deploy
(`tags.product=='market-lake' && tags.environment==<env>`), and surveys the
service repository's manifests (`az acr manifest list-metadata`, one bounded
server page, re-sorted newest-first before any decision). Kept, in precedence
order (the union of all rules is retained):

1. The **newest two** digests (latest + previous) are always kept.
2. The **deployed digest** passed as the `image-digest` input is kept.
3. Any digest **referenced by an active Container App revision or by a
   revision that still receives traffic** (a rollback candidate) of the api,
   quant-api, rth-trader, and optional crypto-trader apps is kept.
4. The current **ingest and backtest job images** (jobs have no revisions;
   their template image is the live reference) are kept.
5. Any digest **carrying a non-40-hex (mutable) tag** is never deleted,
   mirroring the GHCR moving-tag safety net.

Everything else inside the surveyed page is deleted with
`az acr repository delete --yes`, one bounded loop capped at 100 deletions,
per-manifest failures reported as warnings and retried on the next run.
The workflow only ever enumerates and deletes inside the one service
repository, so **non-service images in the registry are never touched**.
Deleting a digest removes its commit-SHA tag along with the manifest, exactly
like GHCR.

### Run Manually

```powershell
gh workflow run prune-acr.yml
gh run watch --workflow prune-acr.yml
```

Optionally pin a digest to retain regardless of age:

```powershell
gh workflow run prune-acr.yml -f image-digest=sha256:<64-hex>
```

The log must report the surveyed manifest count and the deletion count,
ending in a notice like
`ACR prune complete: surveyed N mirrored manifest(s) in fca; deleted D/N; ...`.

### Identity and Permissions

The prune runs environment-gated with `contents: read` + `id-token: write`
and no other write scope. Manifest listing is read-only data-plane access,
which the deployer already exercises in `deploy-main.yml` when it verifies
the mirrored digest. Deletion is a data-plane delete under the same identity;
individual failures are warnings and retry on the next run. If every delete
fails with an authorization error, the deployer identity needs the ACR
delete data action (for example `AcrDelete` or
`Container Registry Repository Contributor`) granted at the registry scope
through the authorized bootstrap path; the runtime and quant identities keep
pull-only access.

### Adoption Notes (Quant Runtime and Thesis Backend)

The workflow is service-parameterized: the `repository-name` input defaults to
the lowercased GitHub repository (the FCA service repo) and can be overridden
for any other ACR repository. When the quant runtime image publication
(T112-T114) and the Thesis backend image deployment (T174, T178) land:

- Import into the private ACR and call `prune-acr.yml` from the owning
  deployment workflow with that service's repository name and digest, or rely
  on the weekly scheduled run if the image deploys outside `deploy-main.yml`.
- Keep the newest-two rule and the revision-reference check unchanged; only
  the repository name and the list of workloads queried for revisions need to
  reflect the adopting service.
