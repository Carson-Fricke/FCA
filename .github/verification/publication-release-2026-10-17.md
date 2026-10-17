# Publication release and development-data cutover

## Scope

The source batch includes `5120b06` and its final Azure-listing/native-build
follow-up: immutable fenced publication, worker resource admission and metrics,
retirement of the Python lake client/soak driver, and deployment/packaging updates.

## Data cutover

On 2026-10-17, accepted Azure ingestion records were verified terminal and no
ingestion workers were active. The Azure dispatcher was changed from scheduled
to manual, the old harness revision was deactivated, and the local harness was
stopped before cleanup.

- Azure: 3,867 Parquet files, 3,869 coverage rows, 12 retained root/partition job
  documents and obsolete publication/control locks were removed. Including HNS
  directory entries, 14,944 storage entries and 126,108,303 bytes were deleted.
- Local: the marker-protected owned lake directory was reset, removing three
  Parquet files, three coverage files and obsolete worker/admission state.
- The request-budget ledger was preserved. Other tables (`backtests`,
  `contracts`, `jobs`, `killswitch`), harness results/state containers and
  conversations were untouched. Final enumeration found no market objects or
  coverage rows; only the retained budget and its parent directories remained.

The cutover does not migrate reads or retain an old-format implementation.

## Verification

| Check | Result |
| --- | --- |
| Python full suite | 1,665 passed, 1 skipped; 128.19 seconds |
| Mandatory delivery architecture guard after final edits | 68 passed |
| Lake workspace after final native alignment/storage fix | 128 passed |
| Harness workspace with the pinned native engine | 318 passed |
| Both workspaces, final all-target Clippy and format | Passed |
| UV lock, repository Ruff lint/format and ty | Passed |
| UI checks, tests and production build | Passed; 280 tests passed, 1 skipped; two existing check warnings |
| Bicep build and lint | Passed with existing template warnings |
| Workflow selection | 6 tests passed |
| Workflow structure/shell parsing | 8 YAML workflows and 63 embedded shell blocks passed |
| Both Dockerfile build checks | Passed without warnings |
| Linux container dependency planner | Built; both recipes include the new pipeline crate and matching native engine versions |
| Linux resource-pipeline Clippy and behavior tests | Passed; 2 tests |
| Installed wheel SDK smoke | Passed in an isolated environment outside checkout imports |

The full harness pass preceded the final Azure directory-filter follow-up; that
behavior was verified through the real SDK against fake HTTP and followed by
both final all-target lint passes. The final lake workspace pass includes it.
Automated provider/Azure services are fake. A separate read-only Azure listing
confirmed that the live HNS account reports directory entries as
`ResourceType=directory`.

Windows native dependencies rebuilt after loading the installed MSVC environment;
this avoids the earlier missing-`msvcrt.lib` linker failures. The direct wheel
build in the large development checkout hit its 120-second discovery bound.
Packaging was then isolated to a clean snapshot of the staged source, matching
Actions checkout inputs: it built in 19.05 seconds, and the installed SDK graph,
identity, references and templates passed. No tests were skipped to obtain these
results beyond the existing opt-in live-Cosmos/UI cases.

## Build performance changes

- Removed 213 retired BuildKit Actions cache entries, reclaiming 9.56 GiB while
  preserving the three Rust/Python caches (1.18 GiB). Current image workflows
  use registry-backed caches instead of those obsolete Actions entries.
- The workspaces previously resolved DuckDB/native engines to different
  versions. The query manifest now pins `1.10506.0`, already used by the harness,
  and the lake lockfile also aligns its native build dependencies, including
  `zopfli`. The native dependency/feature sets match across both workspaces.
  Final lake verification reused the harness native build without recompiling
  `libduckdb-sys`.
- Both generated production recipes resolve `duckdb` and `libduckdb-sys` to
  `1.10506.0`. Dependency cooks, filtered Python inputs, linked runtime payload
  layers and selective workflow jobs remain enabled. The first hosted registry
  cache fill can still be cold; local evidence is not a hosted timing claim.

## Rollout boundary

GitHub billing previously prevented runner startup; the operator reports that
block cleared. The rollout must establish matching harness, worker and dispatcher
images before the Thesis workflow reapplies the shared composition. Automatic
Thesis deployment is temporarily suspended for this controlled rollout and is
restored afterward. Hosted outcomes and deployed image pins are recorded after
the Actions runs complete; the preflight alone does not establish deployment.

## First hosted rollout

[CI](https://github.com/Carson-Fricke/FCA/actions/runs/39061177251) passed on
`36308d9`: Python quality/tests/package took 73 seconds (tests 52 seconds,
wheel and installed-SDK checks one second each), UI took 58 seconds, lake Rust
647 seconds and harness Rust 568 seconds, including compilation/cache fill.

The [first deployment](https://github.com/Carson-Fricke/FCA/actions/runs/39061177296)
published both images and successfully deployed the matching API, worker and
scheduled dispatcher. The first shared-image registry cache fill took 26m 57s;
the finite ingestion image reused that cache and took 12 seconds. Production
compiled DuckDB once, in the harness cook. Native version alignment improves
consistency and cross-workspace verification reuse; it did not eliminate a
second production DuckDB compilation.

The final backtest smoke failed before market-data acquisition: the runtime
copied the minimal base Rustup snapshot, whereas the JIT workspace pin also
requires Clippy and rustfmt. Rustup attempted to install those missing components
under the unprivileged runtime user and failed on its read-only home. The image
now copies the completed build's toolchain and exercises the actual workspace
pin as the runtime user with networking disabled. The smoke workflow's report
strings also had double-escaped backticks that invoked shell substitutions;
those are corrected. A follow-up hosted build verifies the image fix and warm
dependency-cache behavior before the release is considered complete.

The follow-up image build passed its offline rootless probe. Shared-image build
time fell to 4m 04s (dependency cooks were cache hits, no native recompilation);
the finite image took 5 seconds. Follow-up CI passed: Python 100 seconds, harness
143 seconds and lake 106 seconds. Thesis backend, runtime smoke and UI deployment
also passed in [their run](https://github.com/Carson-Fricke/FCA/actions/runs/39063704748).

The fresh-data smoke then exposed a separate registry cleanup defect: retained
OCI indexes did not protect their untagged child manifests. Azure retained the
index but could not pull its platform image. `acr-retention.mjs` now computes
graph closure over hydrated index metadata before producing any deletion plan;
newest-image selection counts roots, and cache grace windows protect children too.
Four focused policy tests cover shared/nested children, explicit retention roots,
incomplete-graph rejection and bounded oldest-first deletion. A temporary manual
Actions recovery restores the exact current published graphs using its package-read
identity; it is removed after that bounded operation. Local GitHub credentials lack
package-read scope and cannot perform that recovery directly.

The completed recovery restored the current harness/ingestion and previous
harness graphs. Thesis images are published directly to ACR, so its broken
previous index could not be restored from GHCR. Its healthy replacement took
100% traffic; the unused old revision was deactivated and that broken index
was archived and removed after checking every current app/job image reference.
Two smoke ingestion executions that never started after image-pull failures
were explicitly stopped, their four durable failed records archived and reset,
and the corresponding backtest stopped before retrying. No market payload or
request-budget entry was deleted during this smoke-only recovery.

ACR and weekly GHCR cleanup now share `image-retention.mjs`. Both hydrate OCI
index references, retain complete graphs and plan all bounded deletions before
applying them. GHCR retains its seven-day floor and ten newest image roots;
its descriptor reads run with bounded concurrency. Temporary recovery code was
removed after use. The final all-cache-hit publish took 6 seconds for the harness
and 5 seconds for ingestion; this measures image publication, not Azure rollout.

Ignored command logs and cutover inventories are under
`.local/verification/release-2026-10-17/` and
`.local/verification/market-cutover-2026-10-17/`.
