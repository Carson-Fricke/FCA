# Partitioned ingestion: local and Azure rollout

## Deployed source and allocations

Application source is `81162c3cf230d40f2f7ecf9eb9c652d6cd9538dc`, pushed to
`master`. Azure deployment `aca-fleet-publication-20261015` succeeded. The ready
API revision is `fca-ml-dev-quant-api--0000077`.

| Component | Configuration |
| --- | --- |
| Main API | 1 vCPU / 2 GiB |
| Manual ACA ingestion Job | 4 vCPU / 8 GiB per execution, no platform retries |
| Fleet | Eight concurrent executions; two sessions per worker |
| Date partition | 31 calendar days by default; configurable for new admissions |
| Scheduled coordinator | 0.25 vCPU / 0.5 GiB, once per minute, provider-free |

The shared image is
`fcau7enxjst4w72u.azurecr.io/fca-harness@sha256:a85c358966aac7449bd4c17a3150b918b66decf1694c97190d30df5a0af44a2d`.
The finite worker/dispatcher image is
`fcau7enxjst4w72u.azurecr.io/fca-ingestion@sha256:d6b1cb1e487edaeb049bf9804146b9cf4bca3fdf26b8c0a68126416298e181e1`.
Registry digests and deployed resources were checked after publication.

One accepted plan retains one public job ID; internal date partitions become
separate finite ACA executions or local child processes. Shared rolling request
reservations enforce at most 10,000 Alpaca attempts/minute per environment,
including retries. Local and Azure remain independent and must not ingest
simultaneously with the same allowance, as requested by the operator.

## Cutover and correctness

Old writers were stopped before deleting exactly three obsolete local and three
obsolete Azure ACI control records. Original control bytes and ETags were backed
up. Canonical market-data formats did not change. Only the owned MSFT benchmark
objects/coverage were reset for fresh acquisition, using validated content and
conditional deletes. No existing crypto coverage was found locally or remotely.

The quant identity now has Container Apps Jobs Operator access. Its retired ACI
Contributor and Managed Identity Operator assignments were removed. No ACI
containers remain. The worker uses its separate runtime identity; provider
credentials are absent from durable work and command arguments.

The first real ACA smoke exposed normal publication contention: all datasets
share one symbol/day lock, Azure acquisition failed immediately with 409, and
the bar job failed while trades and quotes completed. The shared committer now
waits for acquisition ownership before any mutation. It retries only acquisition
conflicts, defaults to a 75-second retry window, and never replays publication or
provider work. The failed job remains terminal; final verification used explicit
new benchmark IDs.

Range verification also fixed two correctness problems: bars keep one range
fetch but publish each session under its own symbol/day atomic boundary; closed
crypto days publish `[open, close)` so midnight ticks belong to one day. Partial
query endpoints before midnight remain inclusive. Crypto coverage counts
published rows, and derived bars use the same day boundary.

## Verification revisions

The repository-wide verification belongs to source revision `fd5691d`, recorded
immutably in [the source verification record](ingestion-fleet-source-2026-10-15.md).
It includes the required Thesis delivery and execution-identity behavior guards,
1,675 Python tests, 115 lake Rust tests, 318 harness Rust tests, 280 UI tests,
Ruff, ty, packaging, workflow and infrastructure checks. Python's final run was
180.10 seconds under concurrent native/image work; the strict three-minute timing
ceiling was narrowly exceeded and is not claimed as met.

Focused verification after the subsequent changes:

- Adaptive permit batching: three worker state checks, worker Clippy and real
  local multiprocess ingestion/restart behavior passed.
- Final publication/range changes: six bars, four columnar and two leased
  behavior tests passed in 0.11 seconds after compilation; ingestion all-target
  Clippy passed in 3.41 seconds.
- The real finite local-worker admission/publication/restart behavior passed in
  5.06 seconds at the final source.
- Eleven ACA HTTP/configuration behavior tests, changed-package formatting,
  Bicep build and diff checks passed after the 31-day default change.
- Both final Linux images passed packaged worker/dispatcher, CLI, SDK,
  toolchain, permissions and API/market-data/MCP health smokes.

No third repository-wide verification pass was run, following the repository's
integration-run limit. Focused follow-ups and operational smokes are distinguished
from the earlier full-source verification.

## Measured acquisition and reading

These are fresh acquisitions of the same closed MSFT session, 2025-12-30, with
three concurrent trade/quote/bar requests. Local and Azure ran sequentially.
The APIs were healthy before admission; ingestion times include finite worker
startup, credentials, acquisition, publication and completion observation.

| Ingestion | Earlier sample | Final sample |
| --- | ---: | ---: |
| Local, all three datasets | 4.338 s | 3.031 s |
| Azure, all three datasets | 58.828 s | 35.047 s |

Final individual completion times were local trades 3.031 s, quotes 2.906 s,
bars 1.609 s; Azure trades 34.313 s, quotes 35.047 s, bars 30.547 s. Azure
admission took 0.313–0.422 s. The final Azure run completed with 67/67 successful
API health observations. All six ACA executions from the two live attempts were
stopped after durable settlement; final benchmark roots confirm worker cleanup.
The final local harness restart and existing Thesis/UI listeners were healthy.

Both environments generated the original 367,988 trade rows, 201,630 quote rows
and 390 bars, with identical Parquet bytes, checksums and snapshot manifests.

| Fresh application-cache reads | Trades | Quotes | Bars |
| --- | ---: | ---: | ---: |
| Local native | 48.4 ms | 23.9 ms | 3.8 ms |
| Azure native, inside API container | 214.7 ms | 133.8 ms | 137.4 ms |
| Desktop HTTP to Azure | 975.6 ms | 194.1 ms | 201.0 ms |

Subsequent decoded-cache visits were approximately 0.007–0.062 ms. These timers
visit verified Arrow batches and count rows; they do not execute a strategy per
event. Cold means fresh application caches, not flushed OS or Azure caches.
Native region-local and desktop WAN measurements are separate. The single-file
cold read sample does not establish a read-speed improvement; ordered prefetch
targets multi-partition snapshots instead.

The earlier three offline CPU replays produced byte-identical Parquet with
trade staging 0.161–0.162 s versus 0.893 s previously, and quote staging
0.098–0.105 s versus 0.674 s. These approximately 5.5x and 6.4–6.9x CPU gains
must not be presented as fleet/network throughput gains.

The worker log streams connected but retained only connection messages. Log
Analytics public queries are disabled. Therefore this run does not provide a
reliable Azure provider-fetch/publication breakdown. Container connections
arrived roughly 17–19 seconds after admission, supporting substantial startup
cost, but the remaining time is not attributed without further evidence.
No month/year sustained-throughput claim follows from this bounded smoke.

## Build and Actions evidence

| Build | Final source rebuild | Compressed upload beyond prior image |
| --- | ---: | ---: |
| Finite ingestion | 71.23 s | 12.98 MiB |
| Shared harness | 142.25 s | 130.15 MiB |

Both expensive dependency cooks were cached. The shared image reused 16/21
layers; Python, Cargo, Rustup, SDK crates and lake CLI layers were byte-identical.
A fully cached rebuild of the preceding linked-layer image took 2.51 seconds.
This is local Docker evidence, not a GitHub-hosted timing claim. The first cold
shared build had taken about 34 minutes, chiefly the DuckDB dependency cook.
Published temporary OCI archives were removed; build caches and raw evidence
were retained under ignored `.local/` directories.

The new source push triggered [CI](https://github.com/Carson-Fricke/FCA/actions/runs/38856211909),
[main deployment](https://github.com/Carson-Fricke/FCA/actions/runs/38856211884) and
[Thesis deployment](https://github.com/Carson-Fricke/FCA/actions/runs/38856211891).
All were blocked before runner startup by the account annotation:

> The job was not started because recent account payments have failed or your spending limit needs to be increased.

Account billing must be corrected in GitHub settings before hosted verification
can run. Build caches and workflow selection cannot repair that account block.
The successful Azure deployment above was performed directly with the verified
images and current Bicep template; no hosted green run is asserted.

One separate progress-reporting limitation remains: root session totals currently
sum only partitions that have emitted progress, so intermediate percentages may
underestimate a queued plan's denominator. Terminal completion and fleet limits
remain correct. This was recorded without expanding the performance task into a
progress API/UI contract change.

Raw final reports are retained in ignored
`.local/verification/aca-fleet/runs/final-publication/`; the region-native JSON was
retrieved without terminal wrapping and checked against the same baseline.
