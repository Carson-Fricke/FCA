# Harness / market-data consolidation verification — 2026-10-15

## Scope and source

Source commits: `013fce0`, `bdc9618`, `86628b8`, `ce8b4b0`, `fb71e10`,
`6b61bb7`, `75acd6c`, `893fd4a`, `7105b47`, `56a4074`. The runtime composes one
complete local or Azure harness. Native execution receives market-data ports;
separate clients use authenticated admission/catalog/snapshot HTTP and download
immutable Parquet objects. Provider execution belongs to finite workers.

Local workers use the same admission, ownership, reconciliation, publication,
coverage, provider and reader contracts as ACI workers. Local adapters supply a
partition-indexed filesystem catalog, atomic job documents, kernel ownership,
durable launch receipts and content-pinned child executables. Azure supplies
Blob/Table storage, leases and ACI. The provider-free scheduled ACA dispatcher
allows the API to scale to zero without abandoning accepted work.

Old local market-data state was explicitly removed with the repository's
marker-protected `lake local reset --yes --data-dir ...` command before the
new local harness started. No historical-format reader or runtime converter
was added. Provider credentials were compared in memory and matched between
the local ignored environment file and Azure Key Vault; credentials were not
printed or included in benchmark artifacts.

## Verification

- `uv lock --check`, repository Ruff lint/format and `ty check` passed.
- Final local Python integration run: **1,675 passed, 1 skipped, 128.86 s**.
  The skip is the explicitly opt-in live Cosmos latency acceptance test.
  The required delivery architecture guard ran in this suite.
- Final-source Linux CI Python run: **1,675 passed, 1 skipped, 77.74 s**.
- [CI at `893fd4a`](https://github.com/Carson-Fricke/FCA/actions/runs/38815109573)
  passed Python quality/tests/build, both Rust workspace format/lint/test jobs,
  and Bicep validation.
- [Final-source Thesis release](https://github.com/Carson-Fricke/FCA/actions/runs/38815109512)
  passed image publication, deployment, runtime smoke and UI deployment.
- A first local Python run overlapped compiler jobs: a streaming test's
  two-second synchronization wait expired. It passed in isolation with a
  60-second process timeout; the final suite passed after build load cleared.
- A cold Linux harness CI job exceeded its former 25-minute overall build
  budget without a failing test assertion. Its remaining JIT compilation
  behavior suite passed locally: **6 tests, 52.71 s**, with a 180-second process
  timeout. CI now caches Rust dependencies and allows a 40-minute cold build.
- Rust format and affected-package Clippy checks passed. Focused behavior
  suites passed for HTTP, coverage, query, shared reading, durable ACI control,
  the real local finite worker, harness entrypoints, native strategy/study
  validation, and native/HTTP clients.
- Final-source format and Clippy passed for **both complete Rust workspaces**.
  The complete market-lake workspace tests passed. The Windows harness build
  first required an explicit installed MSVC environment; native dependencies
  were then rebuilt successfully. Its aggregate test run exhausted a 180-second
  process budget after the API HTTP parity suite passed **41 tests in 141.70 s**.
  The unfinished targets were isolated rather than rerunning completed suites:
  SDK metadata, strategy validation, study validation, mixed study/strategy/
  indicators, and the remaining SDK/live/market-data/MCP/numeric packages all
  passed with individual hard timeouts. No test assertion failed in that final
  harness verification. This cold Windows Rust/JIT verification is **not** a
  claim that the complete native test workload fits under three minutes; the
  Python full-suite timings above remain below that threshold.
  Final harness workspace documentation tests also passed.
- The finite local-worker test exercises real child execution against fake
  external provider HTTP, all three dataset publications, concurrent duplicate
  admission, conflicting input, ownership fencing, immutable terminal results,
  native/HTTP read equality, signed local object downloads and unknown-option
  rejection before admission.
- Bicep compilation/lint and PowerShell parsing passed. Existing Bicep
  conditional-resource warnings remain. Actionlint 1.7.12 passed with its known
  unsupported `concurrency.queue` syntax diagnostic excluded; that syntax is
  already used by the repository's successful GitHub workflows.
- A Linux runtime image built locally. GitHub builds and deploys the final
  immutable image; a locally built intermediate image is not deployment proof.
- The packaged Linux runtime completed real local ingestion with its bundled
  finite worker and filesystem catalog, without Azure. All three snapshot
  manifests exactly matched Windows. Acquisition took **3.225 s**; initial
  native reads took **63.7 ms trades**, **29.2 ms quotes**, **1.3 ms bars**.
  Both harness and market-data health endpoints returned 200. The temporary
  local smoke container was removed afterward.

## Benchmark method

One closed SIP equity session: **MSFT, 2025-12-30, 14:30–21:00 UTC**. Trades,
quotes and one-minute bars were missing before acquisition. Local native
benchmarks used an isolated marked data root; the running local HTTP harness
was tested separately after its explicit data cutover. Real acquisition was
performed before read timing. The Rust `market-data-profile` operator binary
retains job IDs and exports only credential-free snapshot manifests.
Azure acquisition starts from a warm API and newly created finite ACI groups;
it does not measure a separate ACA API scale-from-zero delay. Queueing, ACI
startup and worker execution are reported separately where captured.

The workstation has an AMD Ryzen 9 5900HX, 8 physical / 16 logical CPUs.
Local worker processes are not restricted to the Azure CPU allocation. Each
deployment permits two ingestion workers and divides the configured provider
budget between them. Provider page fetching and CPU parsing remain parallel;
complete-session concurrency defaults to one per worker to bound dense-session
memory growth.

Read timings measure checksum/schema-verified Arrow batch visits with a
row-count callback, not a full backtest or per-event business computation.
"Cold" means a fresh application disk/decoded cache; filesystem and operating
system caches may be warm. Warm visits reuse immutable decoded batches and
execution-pinned manifests.

## Local measurements

| Dataset | Rows | Compressed bytes | Worker execution | Cold native Arrow read | Warm native read |
| --- | ---: | ---: | ---: | ---: | ---: |
| Trades | 367,988 | 3,414,916 | 3.164 s | 135.5 ms | 0.024–0.112 ms |
| Quotes | 201,630 | 1,385,342 | 2.362 s | 54.1 ms | 0.018–0.091 ms |
| 1-minute bars | 390 | 18,136 | 0.467 s | 6.8 ms | 0.017–0.052 ms |

All-dataset native acquisition improved from **7.050 s** to **4.338 s** after
reducing the client completion-poll ceiling from five seconds to 500 ms and
local process reconciliation from two seconds to 250 ms. Worker execution
times are from structured worker start/settlement logs; end-to-end time also
includes queueing, process startup and completion observation. These are
individual measurements, not a statistical throughput guarantee.

Local provider fetch timing was **2.164 s trades** and **1.613 s quotes**;
staging took **0.893 s** and **0.674 s** respectively. Staging includes record
validation/conversion, Arrow normalization, Parquet encoding and checksumming.

The running local HTTP harness acquired all three datasets in **4.847 s**.
Admission took **17–26 ms**. Cold HTTP reads took **76.3 ms trades**, **24.3 ms
quotes**, and **4.1 ms bars**. Its row counts and complete snapshot manifests,
including checksums, exactly matched the native acquisition. Different provider
request ordering and sample timing mean the native/HTTP totals are not a
controlled measurement of transport overhead.

## Azure deployment and measurements

Live verification found and corrected three Azure-specific failures that local
fake boundaries had not exposed: the coordinator's original ACA job name
exceeded 32 characters; conditional blob creation returned specific
`409 BlobAlreadyExists` responses; and empty Blob/lease PUTs required an explicit
`Content-Length: 0`. The Azure HTTP boundary now enforces the latter two
contracts, and all five ownership/admission/reconciliation tests pass.
An image-check JMESPath projection in both deployment workflows was also fixed
and verified against the actual Azure response. Main and Thesis releases of
`75acd6c`, including their runtime smoke tests and UI deployment, passed.

Final-source image publication succeeded, but GitHub refused to start the
deployment job with the annotation: "recent account payments have failed or
your spending limit needs to be increased." This is an account billing block,
not a code/test failure. No billing setting, credential scope or spending limit
was changed. Direct operator deployment uses images built from the same
CI-verified commit, mirrored into private ACR and pinned by their actual
registry digests.

The initial failed deployment was recovered using the already-published pinned
images and corrected Bicep. Two failed benchmark preflights produced queued
records but no workers or provider requests. Only those three known benchmark
records were backed up and removed after verifying `dispatch_started=false`
and confirming that their deterministic ACI groups did not exist. This reset
did not replay any unknown or running provider operation.

The first real ACI run completed trades, quotes and bars with the required
4-vCPU/8-GiB allocations. Its complete snapshot manifests matched local byte
for byte. It also exposed API scheduler starvation at 1 vCPU: synchronous
Blob/Table SDK bridges blocked the Tokio worker while shared credential I/O
and other requests needed that scheduler. The existing SDK concurrency tests
were strengthened to run callers on one Tokio worker; the Table test failed
before the fix. Both SDK bridges now hand scheduler work off while waiting.
All **17 affected storage/catalog/ingestion-control tests**, Rust format and
affected-package Clippy checks passed afterward. Native Azure reads before
the final rollout took 178.1 ms trades, 149.5 ms quotes and 123.0 ms bars.

GitHub refused to start the later CI/deployment jobs for the same account
billing reason. The final Rust change therefore has local executable test and
image-build verification, in addition to the preceding complete green CI run;
the blocked jobs are not represented as passing.

Final operator deployment `codex-market-sdk-final-20261015` succeeded. The
ready API revision is **`fca-ml-dev-quant-api--0000075`**, with **1 vCPU / 2 GiB**
and zero minimum replicas. Actual ACI instance records confirmed **4 vCPU /
8 GiB** for each of the three ingestion groups. West US 2's ten-standard-core
ACI quota accommodates the configured two-worker pool. Blob storage is
**Standard LRS, Hot, West US 2**, colocated with the API and ingestion.

Pinned runtime images, built from final source `56a4074`:

- Harness: `fca-harness@sha256:9d887eee2c17bb495af0dedfe77290c8e49dad587dbc1a6f0db99abe5e1081d2`.
- Ingestion: `fca-ingestion@sha256:166de03167bb6164339604f72e396f441353ce72c87bea6b91002c33729b700c`.

The final cold run used fresh explicit job IDs, retained to disk before
submission. Its HTTP driver validated each acknowledgement and reconciled by
the retained ID after any unknown admission outcome. The Rust profiling binary
then measured verified reads. Before this repeat, only the task's three
completed benchmark jobs, matching coverage entities and checksum-verified
Parquet objects were backed up and conditionally removed using ETags. Their
ACI groups had already been deleted. No other market-data session was reset.

| Dataset | ACI worker execution | Queue before dispatch | Dispatch to worker start | Cold native Azure read | Warm native Azure read |
| --- | ---: | ---: | ---: | ---: | ---: |
| Trades | 5.101 s | 29.079 s | 24.061 s | 163.7 ms | 0.015–0.051 ms |
| Quotes | 3.960 s | 0.084 s | 21.091 s | 123.1 ms | 0.012–0.036 ms |
| 1-minute bars | 1.525 s | 2.658 s | 21.317 s | 124.2 ms | 0.008–0.026 ms |

All three cold acquisitions completed in **58.828 s**, versus **4.338 s** for
local native execution. Azure admission took **344–406 ms**. Quotes and bars
occupied the first two worker slots; trades waited for a slot and a second
container startup. API health stayed responsive throughout acquisition and
the subsequent reads: **129/129 HTTP 200**, **109 ms p95**, **157 ms maximum**
as observed from the workstation.

The workstation's fresh-cache HTTP/signed-Blob reads took **1.228 s trades**,
**250.1 ms quotes**, and **159.6 ms bars**. Warm batch visits took 10–34 μs.
These WAN timings include a different network path from native Azure reads.
All complete manifests, row counts, byte sizes and checksums exactly matched
the local native, local HTTP and packaged Linux-local results.

### What makes Azure slower for this small cold workload

Container startup is the largest cost: **21–24 s per group**, with two waves
under the two-worker limit. This is separate from provider ingestion and cannot
be removed by increasing the main API's CPU. Existing multi-session date-range
jobs amortize startup across more data; covered requests and pinned snapshots
reuse the existing artifacts without launching ingestion.

Provider fetching itself was **1.868 s trades** and **1.629 s quotes**, compared
with **2.164 s** and **1.613 s** locally. This sample shows a modest trade-fetch
advantage and approximately equal quote fetching, rather than a large Azure
ingress advantage. Whole worker execution also includes identity/secret access,
leases, catalog work, publication and durable settlement.

CPU staging was slower on the measured ACI workers: **1.895 s trades** and
**1.295 s quotes**, compared with **0.893 s** and **0.674 s** locally. Validation
and record-to-row conversion accounted for **1.219 s** and **0.834 s** in Azure.
This is a measured optimization target within ingestion, independent of the
reader/host boundary. The measurements do not isolate hardware versus allocator
effects, and the workstation is not restricted to the ACI CPU allocation.

### Deployment and cleanup checks

- Both deployed health endpoints returned 200.
- The API retains its zero-minimum-replica setting. A final probe after an
  idle period first exceeded a 15-second deadline during activation; subsequent
  probes returned 200 and Azure reported a healthy running replica. The warm-API
  acquisition benchmark excludes this separate scale-from-zero latency.
- Bounded backtest execution **`fca-ml-dev-backtest-twdvwdq`** succeeded on the
  final shared image, using the repository's in-image smoke configuration.
- The retired standalone lake API and old ACA ingestion job are absent.
  Provider work runs in ACI; the small scheduled ACA coordinator only dispatches
  and reconciles work.
- All three final ingestion jobs are durably complete, their groups are deleted,
  and no ACI ingestion groups remain.
- Local harness, Thesis and UI remain running. The final local harness restart
  completed successfully with the final Rust source.
- Docker/BuildKit and an ordinary host-side registry upload encountered
  intermittent TLS `bad record MAC`/closed-connection errors. The operator
  rollout used bounded [OCI chunk uploads](https://github.com/opencontainers/distribution-spec/blob/main/spec.md#pushing-a-blob-in-chunks),
  confirmed offsets after interruptions and verified registry manifest digests.
  No TLS verification, billing limit or permission scope was weakened.
