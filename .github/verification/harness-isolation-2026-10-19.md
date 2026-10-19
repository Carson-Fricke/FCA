# Durable harness execution, bounded replay and reusable ingestion workers

Runtime source: `1ea75b84ebb0f8ddeafea8e27c0d9226a461f36c` (including `c8c423a`,
`8d8b1ca`, `704ed6d`, `b65242b`, `ece4de4` and `c3ef4d9`). Operator evidence is
retained under `.local/verification/harness-isolation-2026-10-19/`.

## Changes and guarantees

1. Backtests merge bounded chronological market cursors rather than loading the
   whole requested tick window and preloading the event bus. Exact ledger and
   valuation histories spill to checksummed compressed scratch chunks. Financial
   metrics and Parquet retain the complete history; only chart rendering samples.
   Snapshot fingerprints remain exact. Timestamp regression, cursor and scratch
   bounds fail visibly instead of silently truncating or exhausting API memory.
2. REST and MCP retain the same immutable submission and digest before dispatch.
   Finite local processes or digest-pinned ACA Jobs execute backtests, studies and
   screens. Conditional ownership and heartbeats fence publication. A dead owner
   becomes interrupted and is never automatically replayed. Lost dispatch hints
   leave queued work recoverable; startup reservations bound the whole fleet.
3. Bounded blocking control and artifact-transfer lanes keep synchronous storage,
   MCP and validation work off async request threads. Compiler/loading/constructor
   validation runs in a bounded child; fatal authored code cannot kill the API.
4. Ingestion uses independently claimable disjoint chunks, fair admission between
   roots, coalesced bar windows and warm compatible-worker reuse. Physical
   executor anchors survive chunk completion and bound the fleet through startup
   and cleanup. Provider connections, credentials and governance survive chunk
   handoffs. The shared 10,000-request/minute environment budget remains enforced.
5. Progress separates acquisition, reading, replay and publication. The final
   replay count is exact. Interrupted and unavailable are distinct from active
   ingestion, missing jobs and successful completion in the API and UI.

The API remains 1 vCPU / 2 GiB. Ingestion uses ACA Jobs at 4 vCPU / 8 GiB;
execution workers use configurable ACA Jobs at 4 vCPU / 8 GiB, default fleet two.
Local execution uses the same stores and ownership contracts with finite child
processes. Simulation, reading, provider acquisition, publication and compute
adapters remain separate capabilities. No HTTP self-calls or ACI path were added.

Azure integration also uncovered and corrected deployment/protocol issues:
the scheduled dispatcher name exceeded Azure's length limit; joint Thesis/main
deployments could apply new templates against an older deployed binary; and Job
definition responses contain fields that the Job execution start API rejects.
Starts now project the [documented execution schema](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/jobs/start?view=rest-resource-manager-containerapps-2025-07-01), validate scoped minimal
acknowledgements through exact execution GET, and retain unknown reservations
without replaying a start POST. Release change selection uses the last successful
push deployment, so a failed release's source changes cannot be skipped by a later
infrastructure-only release.
Flat Azure HNS listings also contain physical directory entries with storage-owned
metadata. Job/chunk discovery now filters that physical envelope before requiring
application lifecycle metadata on JSON objects. Fake Azure behavior tests include
directory entries and still reject incomplete job metadata. The first integration
profile discovered this error after successful publication: finite workers failed
while looking for another chunk, preventing warm reuse.

## Verification

- Full local harness and lake format, Clippy and workspace tests passed before
  deployment. Later protocol changes passed focused API unit/behavior checks and
  Clippy. [Final source CI](https://github.com/Carson-Fricke/FCA/actions/runs/39312485721)
  passed both workspaces: 324 harness and 132 lake tests, plus formatting/Clippy.
- The full Python suite passed: 1,665 tests, one live-Cosmos skip. Linux CI took
  66.57 seconds. Windows integration runs under simultaneous development/build
  load took 252.56 and 184.69 seconds; these do not establish an under-three-minute
  Windows result.
- Lockfile, repository Ruff lint/format, ty, UI check/build, affected UI tests,
  Bicep compilation, workflow YAML and release tooling checks passed.
- Retained delivery architecture guards, ingestion interruption/acknowledgement/
  duplicate-admission behavior and local real-worker tests passed. Execution
  behavior covers admission conflicts/joins, retained input, unknown outcomes,
  owner expiry, fleet reservations, artifact streaming and responsive health.
- A real local API restart during reading preserved one execution worker and
  completed `isolation-local-restart-msft-20260730`: 3,012,910 replayed ticks,
  16 artifacts, 56.922 seconds and 293,519,360-byte peak worker working set
  (approximately 280 MiB). No submission or execution was replayed.
- A completed submission joined across MCP and REST, then survived an API restart
  with the same durable result and 16 artifacts.

## Real-data measurement scope

Only MSFT's closed July 30, 2026 session was acquired into isolated owned storage.
Local and Azure ingestion run sequentially because their Alpaca allowance is
shared. Acquisitions start without coverage; reads use the resulting pinned
snapshots. This is one real-provider trial per environment, not a statistical
multi-month speed claim. Earlier two-month results remain in their historical
verification record.

Local acquisition of all three datasets completed in 13.650 seconds; acquisition
plus three reads per dataset took 15.141 seconds. Three chunks used two physical
executors, demonstrating warm reuse.

| Dataset | Rows | Local ensure | Local cold read | Local subsequent reads |
| --- | ---: | ---: | ---: | ---: |
| Trades | 2,063,951 | 13.650 s | 282.3 ms | 264.7 / 268.8 ms |
| Quotes | 948,959 | 5.845 s | 107.0 ms | 0.039 / 0.019 ms |
| One-minute bars | 390 | 2.789 s | 3.835 ms | 0.017 / 0.006 ms |

Trade partitions exceed the decoded cache budget and stream from the compressed
cache on subsequent reads. Quote/bar decoded buffers fit; their microsecond warm
timings represent retained buffer reuse, not a fresh full record conversion.

The first local finite backtest took 84.171 seconds including a 25.56-second
strategy compilation. Its exact fingerprint scan took 10.03 seconds, replay
20.37 seconds and artifact writing 26.61 seconds. Its 16 artifacts totaled
67,243,305 bytes. The later restart proof reused the compiled strategy. Bounded
memory and restart isolation do not remove CPU and scratch I/O costs of exact
per-tick histories, fingerprinting and result materialization; those remain the
measured execution hot spots.

The first Azure integration profile acquired all datasets in 60.615 seconds,
including cold ACA starts and an extra cold quote worker after HNS directory
metadata prevented reuse. Active worker times were 9.000 seconds trades, 4.295
seconds quotes and 2.192 seconds bars; local active times were 10.821, 3.992 and
0.610 seconds respectively. Azure workers had four CPU slots, local workers eight.
Trade/quote ingestion consumed about 10.25/4.44 CPU seconds on Azure. Arrow
projection consumed only 0.191/0.077 seconds; JSON decode used 2.819/1.387 seconds.
Stage durations overlap and network stage totals are aggregate request time.
These one-session measurements do not demonstrate a bandwidth ceiling or an
orders-of-magnitude fleet speedup.

Azure cold reads took 620.5 ms trades, 267.6 ms quotes and 142.2 ms bars. Subsequent
reads took 255.3/235.1 ms trades, 98.7/87.0 ms quotes and 0.024/0.009 ms bars.
Azure retained decoded cache is 128 MiB versus the local default 512 MiB: the
quote partition streams on Azure and remains decoded locally. These cache
results are therefore different resource configurations, not equivalent warm
decode benchmarks. Both environments produced identical row counts, snapshot IDs,
Parquet byte lengths and SHA-256 checksums for all three datasets.

The isolated Azure backtest `isolation-azure-msft-20260730` succeeded with
3,012,910 ticks and 16 artifacts. Admission-to-observation was 137.804 seconds,
including about 53 seconds of worker startup. Compilation took 17.503 seconds,
exact reading/fingerprinting 13.048 seconds, replay 24.626 seconds and publication
28.087 seconds. Its full result, input/ownership evidence and first profile are
retained under `azure-first/` in the operator evidence directory. The temporary
verification Jobs and private container were deleted only after all executions
settled and the request-charge window elapsed.

After the HNS correction, a fresh isolated acquisition of the same closed session
completed in 44.886 seconds (47.022 seconds including all cold/warm reads), using
two physical executors for three chunks. The quote chunk reused the bar executor
and started 46.7 ms after handoff, rather than paying another approximately
25-second ACA startup. All dataset states were complete, and exact snapshots,
row counts, Parquet lengths and checksums again matched local.

| Measurement | Local | Azure first integration | Azure after HNS fix |
| --- | ---: | ---: | ---: |
| All-dataset acquisition, including startup | 13.650 s | 60.615 s | 44.886 s |
| Physical workers / completed chunks | 2 / 3 | 3 / 3 | 2 / 3 |
| Trade active worker time | 10.821 s | 9.000 s | 17.957 s |
| Quote active worker time | 3.992 s | 4.295 s | 3.869 s |
| Bar active worker time | 0.610 s | 2.192 s | 1.689 s |

This is not evidence that Azure is consistently faster than local. The final
trade trial spent 155.861 aggregate seconds in 334 HTTP requests versus 89.845
seconds in the first Azure trial, with identical response-body bytes/rows. Process
CPU remained approximately 9.5–10.2 seconds, and Arrow projection remained below
0.2 seconds. Provider/network waiting varied materially; the rolling-budget
capacity-exhaustion counters stayed zero. Initial executor startup was about
25.1–25.4 seconds. Those waits, followed by exact replay/history output work,
remain distinct from an Arrow bottleneck. Single-session trials do not measure
steady-state throughput of a months-long eight-worker fleet.

Final Azure cold/subsequent reads were 670.9 / 277.7 / 266.8 ms trades,
286.7 / 103.9 / 96.5 ms quotes, and 188.2 / 0.029 / 0.010 ms bars under the same
128-MiB decoded-cache configuration.

## Development cutover

With workers drained, 48 obsolete local ingestion coordination documents and
81 obsolete Azure ingestion coordination documents were backed up and deleted.
Old writers were stopped before current writers resumed. Drained Azure ingestion
Job history was reset with its definition so reused admission references could
not match obsolete vendor executions. Current market publications and immutable
objects, request-budget ledgers and unrelated application tables were preserved.
No runtime compatibility decoder or lazy migration was added.

## Deployed verification

- [Main release and quant smoke](https://github.com/Carson-Fricke/FCA/actions/runs/39312485552)
  applies source `1ea75b8`. The deployed quant smoke job
  `quant-smoke-39312485552-1` succeeded with 3,012,910 replayed ticks and 16 artifacts.
- [Thesis and UI release](https://github.com/Carson-Fricke/FCA/actions/runs/39311605290)
  succeeded, including runtime smoke and hosted UI deployment. Its Thesis/UI
  source is `c8c423a`; the later HNS correction changes only the Rust ingestion
  storage adapter and its behavior coverage.
- API ready/latest revision is `fca-ml-dev-quant-api--0000087`, running
  `fcau7enxjst4w72u.azurecr.io/fca-harness@sha256:9c9d80404a4778cabdeab25f8d24c1d0dd5c26222897f7afef6aaa78983a34c7`
  at 1 vCPU / 2 GiB.
- Ingestion template is
  `fcau7enxjst4w72u.azurecr.io/fca-ingestion@sha256:65a425168829ced76afa5381c51b10a96f41fd6df1f43f602ba24b5f3cd11280`
  at 4 vCPU / 8 GiB. Both small dispatchers run every minute.
- Local Thesis, local UI, rebuilt local harness and Azure harness health checks
  returned HTTP 200. Local Thesis retains its remote Azure harness selection.
- Both isolated Azure profiling scopes were exported and deleted after their
  executions settled and the request-charge windows elapsed. No temporary proof
  Job definitions remain. Production market publications and unrelated user jobs
  were not deleted by verification cleanup.
