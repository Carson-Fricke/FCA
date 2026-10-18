# Ingestion measurement and resource controls

Workers emit JSON `ingestion_metrics` every ten seconds and a final `worker_settled`
snapshot, with the retained `job_id`, internal partition and elapsed wall seconds.
Local logs are under the selected lake root's `ingestion/logs/`; remote events are
in ACA execution stdout/stderr. Final counters are also persisted in each terminal
`ingestion/partitions/<job_id>/<key>.json` document under `metrics`, so execution
cleanup cannot erase them. Counters describe attempted work; job state remains the outcome.

Each stage records calls, service seconds, payload bytes and rows where applicable:
provider-secret setup, coverage discovery, progress persistence, network, JSON
decode, typed normalization, validation, sorting, Arrow projection,
bar derivation, scratch spilling, Parquet, hashing, upload, manifest publication, lease and rate waits.
Rate metrics distinguish cache waiting, authority RPCs, CAS retry backoff, exhausted
authority capacity, and local pacing.
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
| `LAKE_INGEST_MAX_RESPONSE_MIB` | 8 | Maximum decompressed HTTP body; wire admission reserves this maximum, while decode admission uses 16 times actual bytes plus 2 MiB bounded row bookkeeping |
| `LAKE_INGEST_MEMORY_MIB` | 4096 | Session reservation pool |
| `LAKE_INGEST_SESSION_MIB` | 2048 | Reservation for bounded active merge/encoding work, held through completion; page streaming removes a daily row-count limit |
| `LAKE_INGEST_PUBLICATION_CONCURRENCY` | 2 | Storage/lease publications independent of CPU slots |
| `LAKE_INGEST_HOST_MEMORY_MIB` | 75% of currently available RAM locally | Accepted host budget divided among local workers; explicit override available |
| `LAKE_INGEST_SCRATCH_MIB` | 4096 | Owned temporary run/Parquet files; released on deletion and visibly rejected when exhausted |

Local workers divide host CPU slots across the accepted maximum worker count.
Fleet admission caps workers to at least 512 MiB per worker. Each worker allocates
one third of its host-memory share to session reservations and one half (capped at
2048 MiB) to response/decode headroom; the remaining share leaves runtime overhead.
Explicit worker settings override these derived defaults. The accepted host budget
is retained with the immutable execution specification.

Wire and decode are separate pools within the response budget (one quarter wire,
three quarters decode), so queued bodies cannot starve their own decoders. Typed
pages retain decode reservations until acquisition consumes them. Sessions spill
canonical Arrow runs and merge at bounded fan-in; sealed files close write handles.
Final Parquet encoding and local/Azure uploads stream files. Scratch limits fail
rather than waiting for capacity held by that session. The 4-vCPU/8-GiB worker and
1-vCPU/2-GiB API allocations remain unchanged.

Plans expose up to eight balanced finite worker lanes by default. Each consumes
several short, disjoint chunks before exit, amortizing startup and sharing provider
connections. Compare end-to-end admission-to-terminal time separately from active
worker time, and compare durable committed row counts and logical records.

## Plan/admission cutover

The current lane identity hashes its ordered request list, and durable jobs include
a required `metrics` field (null before settlement). Local execution input includes
the accepted host-memory budget. Drain/stop old ingestion writers before replacing
API/dispatcher/worker images together; remove obsolete ingestion job, partition and
executor/admission records within the selected development store. Existing current
market manifests, immutable Parquet, contracts, listings and other application data
remain valid. Preserve the request budget ledger through outstanding charges.
Readers never infer old plan formats or launch old retained worker images.

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

Finite local workers own scratch directories under `ingestion/scratch/<work-key>/`. Process cleanup removes that checked directory after the worker exits, including orphaned files from an interrupted process; system temporary storage is not left with unowned ingestion runs.
