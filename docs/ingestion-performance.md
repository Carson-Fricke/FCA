# Ingestion measurement and resource controls

Workers emit JSON `ingestion_metrics` every ten seconds and a final `worker_settled`
snapshot, with the retained `job_id`, internal partition and elapsed wall seconds.
Local logs are under the selected lake root's `ingestion/logs/`; remote events are
in ACA execution stdout/stderr. Metrics are operational logs, not job outcomes.

Each stage records calls, service seconds, payload bytes and rows where applicable:
provider-secret setup, coverage discovery, progress persistence, network, JSON
decode, typed normalization, validation, sorting, Arrow projection,
bar derivation, Parquet, hashing, upload, manifest publication, lease and rate waits.
Stage service times overlap across tasks and must not be summed as wall time.
Counters describe work at stage boundaries, including attempted uploads; committed
rows and the execution outcome remain the job progress/state authority. Early worker
failures emit `worker_failed` with a final resource snapshot.
Network byte counts are decompressed HTTP body bytes, not compressed wire bytes.
Process CPU seconds, current RSS and peak RSS are separate from budget accounting.
Pool snapshots report active/peak units, queued admissions and completed wait time,
including cancelled waits. Compare counter differences between periodic snapshots
to compute interval throughput and CPU utilization.

## Offline replay

From the repository root:

```powershell
$env:LAKE_INGEST_CPU_SLOTS = '4'
cargo run --manifest-path src/lake/Cargo.toml --release -p lake-ingestion --example replay -- 1000000 3
```

Arguments are rows per dataset and repetitions. Each repetition fetches that many
trades and quotes over one closed synthetic session, checks committed row counts,
and prints JSON with wall time, rows/second, Parquet bytes and cumulative metrics.
It uses the real HTTP/decode/pagination/normalization/Arrow/Parquet/publication path
against a loopback fake provider and isolated temporary local storage. No secrets
or external network are needed. The process also hosts the synthetic provider;
OS CPU/RSS includes it. This is a repeatable software-pipeline benchmark, not a
prediction of Alpaca ingress, Azure Blob throughput or ACA startup. Verify real
ingress only with the bounded closed-symbol/session procedure in the repository
instructions, keeping local and Azure ingestion separate.

## Limits

| Setting | Worker default | Meaning |
| --- | --- | --- |
| `LAKE_INGEST_CPU_SLOTS` | Available CPU allocation; Bicep sets 4 | Shared decode, normalization, sorting and encoding slots |
| `LAKE_INGEST_RESPONSE_MEMORY_MIB` | 1024 | Response body/decoded-page headroom pool |
| `LAKE_INGEST_MAX_RESPONSE_MIB` | 8 | Maximum decompressed HTTP response body; reserves eight times this for its lifetime through normalization |
| `LAKE_INGEST_MEMORY_MIB` | 4096 | Session reservation pool |
| `LAKE_INGEST_SESSION_MIB` | 2048 | Reservation held before fetch through final completion; normalized input is limited to one quarter of this, leaving encoding headroom |
| `LAKE_INGEST_PUBLICATION_CONCURRENCY` | 2 | Storage/lease publications independent of CPU slots |
| `LAKE_INGEST_HOST_MEMORY_MIB` | 32768 locally | Host accounting budget divided among local workers |

Local workers divide host CPU slots across the accepted maximum worker count.
They allocate three quarters of their host-memory share to session reservations,
one quarter (capped at 1024 MiB) to response headroom and divide the session pool by
session concurrency. Explicit worker settings override these derived defaults.
The host budget is configurable; it is not physical-memory autodetection.

These are explicit admission/accounting limits and response/input guards, not a
general RSS allocator. A dataset exceeding retained-input headroom fails visibly;
it does not wait for memory held by itself, silently truncate data, or spill to
disk. A workload requiring larger single sessions needs a reviewed larger budget
or a future spill/streaming design. Keep total reservations below actual memory
with headroom for runtime, SDK, reader and allocator overhead. The 4-vCPU/8-GiB
worker and 1-vCPU/2-GiB API allocations remain unchanged. Bicep exposes the resource
controls together as `ingestionPipelineLimits` and passes them to accepted ACA work.

## Publication cutover

Current market paths are `sha256=<digest>/<logical-partition-path>`. Coverage is
one fenced manifest per asset/symbol/day under `publications/`, containing all
dataset and interval entries. Azure Tables no longer store production coverage.
There is no legacy coverage decoder or mutable-path fallback.

Before deploying this contract, drain accepted ingestion and stop every old API,
dispatcher and worker writer. Reset affected development market coverage, market
objects, saved snapshots and retained ingestion admission/partition/executor
records (which can otherwise join an obsolete completed job or launch its old
pinned image), preserving unrelated contracts, listings, reference
data and harness state. Start matching current API and worker images together;
never run mixed writers. Local cleanup uses the marker-protected lake reset only
when its full owned scope is intended. Remote cleanup must select the retired
coverage partitions and market-data prefixes rather than dropping the shared
metadata table/container. Preserve the request-allowance ledger through its
outstanding charged window; a full local reset must wait for that window to
expire before removing the ledger. Reads reject corrupt/outdated current manifests and
never convert or delete them. Old immutable generations remain retained; garbage
collection needs an explicit snapshot-retention policy.
