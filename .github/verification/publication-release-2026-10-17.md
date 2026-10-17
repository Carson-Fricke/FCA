# Publication release and development-data cutover

## Scope

The source batch includes `5120b06` and its final Azure-listing/native-build
follow-up: immutable fenced publication, worker resource admission and metrics,
retirement of the Python lake client/soak driver, and deployment/packaging updates.

## Outcome

Committed and pushed to `master`. The [main deployment and fresh-data backtest](https://github.com/Carson-Fricke/FCA/actions/runs/39065264294)
and [Thesis backend/runtime/UI deployment](https://github.com/Carson-Fricke/FCA/actions/runs/39065264163)
passed. [Full source CI](https://github.com/Carson-Fricke/FCA/actions/runs/39063975234)
passed; subsequent release-tooling checks include all ten Node assertions.
The [GHCR cleanup](https://github.com/Carson-Fricke/FCA/actions/runs/39066148059)
and [current shared-policy ACR cleanup](https://github.com/Carson-Fricke/FCA/actions/runs/39066425102)
also passed against the real registries. Temporary recovery code is removed and
the Thesis deployment workflow is active.

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
| Release tooling | 10 Node tests passed: 6 workflow selection and 4 graph-retention policies |
| Final workflow structure/shell parsing | 8 YAML workflows and 65 embedded shell blocks passed |
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

GitHub billing previously prevented runner startup; the operator cleared that
block. Matching harness, worker and dispatcher images were installed before
restoring the temporarily suspended Thesis workflow. Both deployment workflows
and their final smokes passed. The evidence below includes hosted outcomes and
live image pins, beyond preflight verification.

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
those are corrected. The follow-up hosted build verified the image fix and warm
dependency-cache behavior.

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

The final scheduled-dispatcher check caught an incomplete part of that reset:
the old stopped ACA executions still correlated with the reused smoke admission
IDs. The unchanged image acknowledgement guard correctly rejected them. After
verifying all ten manual Job executions terminal, an exclusive lease on the shared
dispatch lock fenced both coordinators while the manual ingestion Job was
exported, recreated with its exact image/identity/resources, and verified to have
empty vendor execution history. The lease was then released. Completed admission
results, fresh publications and request allowance were preserved; the temporary
reset helper was removed. The cutover documentation now explicitly includes
vendor history when admission IDs are reset.

ACR and weekly GHCR cleanup now share `image-retention.mjs`. Both hydrate OCI
index references, retain complete graphs and plan all bounded deletions before
applying them. GHCR retains its seven-day floor and ten newest image roots;
its descriptor reads run with bounded concurrency. Temporary recovery code was
removed after use. The final all-cache-hit publish took 6 seconds for the harness
and 5 seconds for ingestion; this measures image publication, not Azure rollout.

## Final live evidence

| Workload | Verified image digest |
| --- | --- |
| Harness API / backtest / traders | `sha256:39b6e6b27b775deb2a42eb6e27dfb7a5dcbb7da53005651d69f8b0864e882523` |
| Ingestion worker / dispatcher | `sha256:8a777b7516775a062d2736ba42f6c9a8fd05e0a690f9f57ccb59ac04b7cf8a5e` |
| Thesis API | `sha256:b02ac914f06a9f9a103287eeb00cf3d6c62f8f91158106de592aac698be39ff0` |

The API allocation remains 1 vCPU / 2 GiB; finite ACA workers remain 4 vCPU /
8 GiB. The dispatcher is scheduled, and both fresh ingestion executions
(`56u8644`, `9epnmmx`) completed. Backtest execution
`fca-ml-dev-backtest-qyeoptg` succeeded with 4,708,592 market events, 2 orders and
15 fills. MSFT trades and quotes for January 2–4, 2024 have complete current
publication manifests and immutable SHA-256 objects. Bars were not requested
by this smoke and remain missing; all-three-dataset behavior is covered by the
retained local finite-worker suite. The quote worker reported 982,191 committed
rows in 24.287 worker seconds; this is an operational smoke measurement, not
a controlled local/Azure throughput comparison.

Local and remote harness `/health` and `/market-data/health` return 200 with
status `ok`; both protected auth routes reject anonymous requests with 401.
Local Thesis health and UI `/w` also return 200. The local harness was resumed
with optimized current binaries after its reset.

Measured hosted build/cache states:

| State | Shared image | Finite ingestion image |
| --- | --- | --- |
| First dependency-cache fill | 26m 57s | 12s |
| Dependency cache reused; source/runtime follow-up | 4m 04s | 5s |
| All image layers reused | 6s | 5s |

The full warm CI run completed in 2m 32s; individual Rust jobs took 2m 23s and
1m 46s. These are measured cache states, not a claim that a cold native build
completes in seconds. Registry graph retention was rechecked after cleanup.

Ignored command logs and cutover inventories are under
`.local/verification/release-2026-10-17/` and
`.local/verification/market-cutover-2026-10-17/`.
