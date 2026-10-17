# Publication guarantees, ingestion instrumentation and resource backpressure

## Source and scope

Verified the working tree relative to `f1ccf33`. This batch changes the publication
contract, shared ingestion resources and operational measurements. It does not
deploy images, restart the live stack, reset market data or call Alpaca/Azure for
real ingestion. Unrelated Python SDK/packaging, workflow and runbook cleanup was
present in the shared working tree and was preserved.

Current architecture: [market-data runtime](../architecture/market-data-runtime.md).
Controls, replay commands and cutover: [ingestion performance](../../docs/ingestion-performance.md).

## Implemented contract

- Market objects use immutable SHA-256-prefixed logical partition paths. A single
  symbol-day manifest is the visibility authority for all dataset/interval rows.
  Failed candidate uploads leave prior coverage and previously pinned bytes intact.
- Azure leases the manifest itself, renews at one third of its finite lifetime,
  and sends the lease ID with the final visibility PUT. Local ownership uses a
  kernel lock and atomic manifest replacement. Stale/released owners cannot publish.
- Selected rows merge under ownership and become visible in one manifest write.
  Failed final acknowledgements are unknown outcomes and never replay mutations.
  Invalid, duplicate, corrupt and missing-object metadata is rejected before writes.
- Retired the mutable per-dataset coverage adapters and JSON catalog implementation;
  there is no legacy coverage reader or object-path upgrade. Native/HTTP readers
  validate current paths, including encoded crypto symbols.
- Long-history metadata reads filter symbol/date paths before download. Azure
  overlaps up to 32 manifest downloads and returns results in request order.
- Providers and transactions share CPU, response/session-memory and publication
  budgets. Blocking work retains reservations; publication waits do not hold CPU
  slots. Cancellation fences final visibility before durable job settlement.
- Workers report secret setup, coverage, progress, network, decode, normalization,
  validation, sorting, Arrow, derivation, Parquet, hashing, lease, upload, manifest
  and allowance waits, with row/byte counters, pool peaks/waits and OS CPU/RSS.
  Early failures also emit structured resource evidence.

Resource limits are admission/accounting limits, with explicit body and normalized
input guards. Oversized individual datasets fail visibly; disk spilling is not
implemented. Historical immutable objects are retained; collection requires a
snapshot-retention policy.

## Verification

Two repository-wide integration passes were performed. No third full suite ran.

| Check | Result |
| --- | --- |
| Python full suite, final pass | **1,665 passed, 1 skipped**, pytest **125.19 s**; command wall time 127.93 s |
| Lake full workspace, final pass | **127 passed**, including the real local worker/native/HTTP smoke |
| Harness full workspace, first pass | **318 passed** before response-budget tuning |
| Both workspaces: all-target Clippy and format | Passed on final integration pass |
| UV lock, repository Ruff lint/format and ty | Passed on final integration pass |
| Bicep compilation | Passed; existing template warnings remain |
| Actions change selector | **6 passed** |
| Diff whitespace | Passed |

The first format check caught a Windows-encoded apostrophe in one README. It was
converted to UTF-8 and the final repository format check passed.

The final harness workspace rerun failed while linking `job_id_receipt` and
`mcp_http`: MSVC could not find the installed `msvcrt.lib` through that shell's
library search paths. Loading `vcvars64.bat` repaired the environment. The native
dependency rebuild hit its 180-second bound; resuming only these two targets from
the warmed cache then passed **5 + 7 tests**. This is a focused recovery, not a
claim that a third complete harness run passed after budget tuning.

A final metadata consistency guard (rows/bytes/checksum require an object reference)
was verified with the focused catalog/publication/snapshot suites. Their publication
cases cover merged concurrent datasets, bounded lease contention, renewal, partial
upload isolation, stale ownership, cancellation, immutable pinned bytes, unknown
acknowledgements and invalid candidates. The HTTP snapshot case verifies corrupt
stored metadata is rejected before signed object access. The Azure SDK HTTP fake
verifies the actual lease header and bounded concurrent downloads.

The local worker smoke additionally checks structured settlement metrics and equal
native/HTTP quote reads for `BTC/USD`, including percent-encoded signed paths.
Automated external services are fake. The opt-in real Cosmos latency case was skipped.

Raw logs are ignored under `.local/verification/publication-2026-10-16/` and
`.local/verification/publication-2026-10-16-final/`.

## Reproducible throughput evidence

The maintained release example ingests **1,000,000 trades + 1,000,000 quotes** per
repetition through loopback HTTP, real decoding/pagination/normalization, Arrow,
Parquet and fenced local publication. Each of three repetitions checks committed
row counts. Ingestion CPU admission is four slots. The synthetic provider runs in
the same process, so OS CPU/RSS includes its work. These are software-pipeline
measurements, not Azure ingress or startup measurements.

| Response headroom pool | Median seconds / 2 million rows | Peak process RSS |
| --- | ---: | ---: |
| Initial 512 MiB | 5.441 | 758.6 MiB |
| 1,024 MiB candidate | 4.066 | 866.7 MiB |
| 2,048 MiB candidate | 3.925 | 826.9 MiB |

The 1-GiB budget removes the initial eight-response cap while retaining more
headroom than the 2-GiB candidate. It is now the worker/Bicep default. Local workers
allocate one quarter of their host-memory share to response headroom, capped at
1 GiB. Container allocations remain **4 vCPU / 8 GiB** for ingestion and
**1 vCPU / 2 GiB** for the main API.

The final default replay returned **3.862, 3.789 and 3.779 seconds**: median
**527,800 rows/s**, peak process RSS **839.4 MiB**. Each repetition produced
2,585,691 Parquet bytes. All resource pools were empty after completion. Across
six million rows, Arrow projection took 0.608 aggregate stage seconds; JSON decode
and normalization took 9.920 and 15.649 seconds. Service timings overlap and must
not be added as wall time. This replay identifies JSON work, rather than Arrow
projection, as the larger CPU opportunity for a later optimization batch.

## Release boundary

This source batch is **not deployed**. Before rollout, drain accepted ingestion,
stop old writers and reset affected development coverage, market objects, snapshots
and retained admission/partition/executor records. Preserve unrelated contracts,
listings, reference data and harness state. Preserve the charged request-allowance
ledger until its outstanding window expires. Start matching current API/worker
images together; do not permit old accepted images to repopulate retired paths.
No read-time migration, mixed-writer compatibility or automatic data deletion exists.
