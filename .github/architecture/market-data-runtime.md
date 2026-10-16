# Harness market-data runtime

## Status

The source implements one harness-hosted HTTP composition, a shared read-only
reader, durable partitioned local/ACA ingestion, native catalog access, and snapshot/cache reuse. Azure rollout is a
separate release operation; source verification is not proof of deployment.

## Boundaries

| Capability | Owner | Responsibility |
| --- | --- | --- |
| Market-data reading | `src/harness/market-reader` | Validate pinned manifests, verify/decode Parquet, bound retained caches, and visit ordered Arrow batches. |
| Harness data acquisition adapter | `src/harness/lakeclient` | Ensure/wait policy, execution-scoped manifest pinning, signed-object transport, progress, and reference/option API calls. |
| Catalog and availability | `src/lake/catalog`, `coverage`, `query` | Classify committed data and plan availability. No provider work occurs during reads. |
| HTTP | `src/lake/http` | Authenticated catalog, admission, snapshot, read and reference routes, composed under the harness API's `/market-data` prefix. |
| Ingestion execution control | `src/lake/ingest-jobs` | Conditional durable admission, immutable worker specifications, lease-fenced job state, ACA execution reconciliation and aggregate worker limits. |
| Finite ingestion process | `src/lake/ingest-worker` | Claim one job, fetch provider secrets using its identity, run shared transactions, publish progress/results, settle and exit. |
| Provider acquisition/publication | `src/lake/providers`, `ingestion`, `storage` | Provider paging/governance, staging, canonical Parquet writing and symbol-day publication leases. |
| Local deployment adapters | `src/lake/local` | Partition-indexed filesystem catalog, durable job documents, kernel ownership and content-pinned finite worker processes. |
| Hosting composition | `src/lake/hosted`, `src/harness/api` | Select local or Azure adapters and inject native capabilities and HTTP routes. The hosted API has no provider-client dependency. |

`harness-market-reader` depends on value contracts and Arrow/Parquet, not HTTP,
Azure, workflows, providers, ingestion or backtest execution. Its only source
port supplies compressed partition bytes. Native execution, separate clients, and ingestion's trade-to-bar derivation all use this same reader.
Bar derivation owns pure Arrow-to-domain projections; storage no longer owns a
second market-data decoder or bulk-data HTTP proxy. Live recorder replay and
reference-data formats retain their own distinct projections.

The backtest kernel remains independent of catalog, provider and Azure APIs.
The client facade composes acquisition and reading without making the reader
responsible for ensuring availability, launching jobs, waiting or executing
programs. Backtests continue to acquire quotes and trades for execution.

## Snapshots and cache reuse

`GET /market-data/v1/snapshot` publishes a canonical request and ordered committed
partitions, including path, SHA-256, rows and compressed bytes. The manifest ID
hashes those facts, excluding access URLs. Execution-scoped client clones pin a
manifest once per request; attaching a new execution reporter starts a fresh
pin scope. Content caches survive scope changes.

The reader coalesces concurrent requests for a partition, verifies bytes and
the exact current Arrow schema, and reuses immutable decoded buffers. Four active partition reads bound candidate decode memory separately. A bounded
LRU retains decoded batches; a bounded checksum-addressed disk cache retains
compressed Parquet across process restarts. Oversized decoded partitions stream
in bounded batches without entering the decoded cache. Cache budgets bound
retained data and entry bookkeeping, including positive weights for empty
partitions; active reads also require compressed payload and decode memory.

`HARNESS_MARKET_DATA_CACHE_DIR` selects the cache directory (default
`.local/market-data-cache`). `HARNESS_MARKET_DATA_CACHE_DISK_BYTES` defaults to
4 GiB and `HARNESS_MARKET_DATA_CACHE_MEMORY_BYTES` to 512 MiB. Zero disables
the corresponding cache. Cache corruption and metadata mismatches fail visibly.

`POST /market-data/v1/snapshot/locations` renews access to the same manifest.
API bearer credentials are never sent to signed-object URLs. If the canonical
object has been replaced, an old snapshot can use previously verified cached
bytes; otherwise checksum validation rejects the replacement. A snapshot pins
content identity, not perpetual retention of historical objects.

## Durable partitioned ingestion

Admission retains immutable requests and the accepted executor specification in
`ingestion/jobs/<job_id>.json`. Identical inputs join the retained execution;
different inputs conflict and terminal outcomes are immutable. Option selection
is one standalone operation; dataset batches may contain multiple requirements.
The API admits and observes work without fetching provider data.

The planner deterministically divides dataset date ranges into disjoint calendar
partitions (31 days by default). Each partition keeps the original execution
`job_id` and an internal content-derived partition key. Its authoritative state
lives at `ingestion/partitions/<job_id>/<key>.json`. These are work identities,
not additional execution job IDs. The root plan and every partition document
are durable before any external start. Partition size and worker specifications
remain immutable after admission.

A renewable dispatcher lease bounds the aggregate fleet across all API replicas
and the scheduled coordinator. Existing partition records are read with bounded
storage concurrency; conditional creation happens only for absent work. Workers
own separate partition leases, so a long plan can execute in several containers
without sharing a mutable progress document. The coordinator aggregates session
progress, failures, completion and cleanup onto the original job. Each worker
rechecks committed coverage before executing its accepted partition.

Azure uses manual ACA Job executions of `<quant-api-name>-ingest`; local mode
uses content-pinned finite child processes through the same ports. Dispatch
intent precedes the non-idempotent ACA start POST. A lost acknowledgement is
reconciled by the existing execution template's exact job, partition and image
attribution. It never authorizes another POST. Missing/running work times out
visibly; a replacement worker cannot replay a partition already marked running.
Duplicate/conflicting admission, mismatched acknowledgements and lost leases
remain executable behavior contracts. Terminal execution history is retained;
active replicas are stopped before their occupied slots are released.

The provider governor acquires its fleet permit after its adaptive in-flight
lane and before each transport attempt, including retries. One shared rolling
10,000-request/minute reservation authority is scoped by a non-secret hash of
the endpoint and credentials. Azure uses conditional Blob ETags and the storage
service's HTTP Date; local processes use kernel-locked atomic files and shared
OS monotonic uptime. A local reboot holds capacity for a full window. Worker
permit lifetimes use monotonic time anchored before the reservation RPC, so
delayed responses cannot extend grants. Workers adapt short permit batches to
their observed consumption, shrinking after unused expiry and growing only when
exhausted before half their lifetime. This avoids wasting allowance on slow
workers without refunding reservations. Unused,
expired and ambiguously acknowledged grants remain charged through sixty seconds
after their last permitted use; restart never refunds possible provider requests.
Budget errors fail closed. There is no fixed division by maximum worker count,
so idle slots do not strand another worker's budget. Local and Azure have
independent authorities and must not ingest simultaneously with the same Alpaca
allowance; this is the operator's current execution policy.

Provider decoding returns typed records. Ingestion validates and canonically
orders them, then builds Arrow batches directly with exact decimal values.
Storage accepts only the pinned schema and serializes canonical Parquet; no JSON
row normalization facade remains. A shared CPU-stage semaphore bounds blocking
validation, encoding and publication, while bounded sessions overlap provider
I/O. No per-column or per-pass thread fan-out multiplies with concurrent sessions.

Publication remains atomic within one symbol/day. Competing dataset writers wait
up to 75 seconds for that publication lease before any mutation; only acquisition
conflicts are retried. Publication, provider acquisition and uncertain ACA starts
are never replayed by this wait. Bar ingestion keeps its bounded range fetch and
publishes each session separately rather than attempting a multi-day commit under
one symbol/day lease. Closed crypto days use half-open tick membership
`[open, close)` so adjacent days cannot duplicate a midnight event; an unfinished
day includes its current query endpoint when that endpoint precedes the close.

## Deployment and cutover

The combined quant API hosts `/market-data` at 1 vCPU / 2 GiB. Azure's finite
ingestion Job uses the narrow worker image at 4 vCPU / 8 GiB on Consumption,
with platform replica retries disabled. Defaults are eight concurrent worker
executions, two sessions per worker, 31-day partitions and an 86,400-second
execution deadline. Bicep exposes `ingestionMaxWorkers`,
`ingestionSessionConcurrency`, `ingestionPartitionDays`, `ingestionCpu`,
`ingestionMemoryGiB`, `ingestionTimeoutSeconds` and `ingestionImageReference`.
Runtime controls are the corresponding `LAKE_INGEST_*` settings; the job name,
worker identity, image, resource group and provider Key Vault are explicit inputs.
The partition default amortizes worker startup over month/year requests. Smaller
partitions favor short-plan fan-out; larger partitions reduce startup waves for
bulk history. Changing the setting affects new admissions only.

The coordinator and API use the quant identity for control. The worker uses the
separately declared runtime identity attached to the ACA Job; coordinator identity
configuration must never override it. The worker retrieves its two provider
secrets concurrently and caches resource-specific ACA managed-identity tokens.
No credentials enter durable work or command arguments.

The 0.25-vCPU / 0.5-GiB scheduled coordinator remains provider-free, reconciling
once per minute when the API is scaled to zero. Active APIs reconcile every two
seconds; local processes reconcile at 250 milliseconds. Caller-owned observation
caps polling at 500 milliseconds. The coordinator receives Jobs Operator access;
the retired ACI administration and identity-attachment grants are removed at
operator cutover. Conditioned GitHub deployer role changes require the authorized
OIDC bootstrap before recurring releases can create the new grant.

ACA replaces the ACI adapter completely. Old ACI job documents are obsolete and
must be drained and deleted before deploying current writers. There is no old
executor decoder or runtime migration. Canonical Parquet and coverage remain
unchanged, including byte-identical output for the retained replay benchmark.
Local reset remains an explicit marker-protected operation; reads never delete
or upgrade outdated data.

`HARNESS_MARKET_DATA_MODE` selects the complete local or Azure composition.
Local mode needs no Azure service. Local Thesis/UI can select the entire remote
harness instead. Native execution receives catalog/acquisition and reader ports
directly; HTTP and signed object URLs serve separate clients. Worker binaries are
content-pinned locally, and per-record catalog writes avoid whole-catalog rewrites.

The shared reader prepares up to two partitions ahead while visiting batches in
snapshot order. Four global preparation slots and a separate speculative budget
protect required head reads. Flight coalescing, checksum/schema/row validation,
bounded disk and decoded caches, and oversized streaming remain shared across
native execution, HTTP clients and bar derivation. Objects above 64 MiB compressed
are read only as required heads, keeping large payloads out of speculative memory.
Cancellation drops speculative
source futures and closes decode channels without retaining a multi-year snapshot.

The finite ingestion image contains neither Python, JIT tools nor DuckDB. Docker
cooks complete symmetric cross-workspace Cargo recipes into stable dependency
layers before restoring sources. The harness branch can reuse compatible lake
artifacts without sharing mutable target mounts with concurrent branches. Python
image inputs preserve the canonical wheel contents while excluding unrelated UI
and non-SDK Rust source. Registry-backed intermediate caches isolate image builds
from Actions' dependency-cache storage allowance.

The harness dependency cook is an immutable `harness-deps` stage. Runtime Cargo
files come from that stage and Rustup from `rust-base`, never from a source build
that mutates toolchain/cache metadata. Linked payload copies keep unrelated image
layers reusable when an earlier payload changes.

## Executable verification

- `harness/market-reader/tests/snapshots.rs`: content identities, corruption
  rejection, concurrent coalescing, restart reuse, disabled budgets and
  empty-partition eviction.
- `harness/lakeclient/tests/streaming.rs`: one manifest per execution, shared
  Arrow buffers, fresh pin scopes and credential isolation.
- `lake/ingest-worker/tests/local.rs`: real finite worker against fake provider HTTP, all three datasets, retained identity/ownership, HTTP admission and native/remote read equivalence.
- `lake/ingest-jobs/tests/unit/azure_http.rs`: real HTTP adapters against fake
  Azure, duplicate/conflicting admission, ambiguous dispatch, worker caps,
  acknowledgement mismatch, renewal during slow control-plane calls and
  expired-lease rejection.
- `lake/http/tests/ensure.rs` and `snapshots.rs`: queued admission, interruption,
  explicit new attempts, option membership, pinned metadata and scoped transport.
- `harness/backtest/tests/lake_parallel.rs` and `options.rs`: unchanged acquisition
  requirements and current option schemas through native execution.

Tests for synchronous API-owned ingestion, terminal-record replacement and local
provider execution are superseded by these boundaries. Provider transaction,
publication, SDK-concurrency and schema tests remain required.

Option membership and canonical option-file classification live in `lake-coverage`; `lake-query::plan_snapshot` is shared by native and HTTP readers. Local and Azure query admission use the same retained-job callback and terminal-state policy.

Local process reconciliation runs at 250 ms; Azure control-plane reconciliation runs at 2 s. Caller-owned completion observation caps polling at 500 ms. These bounds avoid multi-second local completion/handoff delays without copying execution policy.

Native and HTTP option acquisition validate the same contract membership and use one canonical option job identity. Unknown contracts fail before admission in both transports.

Azure conditional blob creation treats `412` and the specific `409 BlobAlreadyExists` response as existing-resource outcomes. Admission validates retained input before joining; coordinator-lock creation then acquires the existing lock. Other conflicts retain their failure/ownership semantics.

Empty Blob and lease PUTs explicitly send `Content-Length: 0`, as required by Azure Storage. The Azure HTTP behavior boundary enforces this requirement for admission/reconciliation and lease acquisition, renewal and release.

The synchronous Blob and Table SDK bridges yield multithreaded Tokio scheduler workers while waiting for their dedicated SDK executors. A one-worker API must remain live while storage operations and shared credential I/O complete. The SDK HTTP concurrency behavior tests run on a single Tokio worker and verify overlapping operations with independent outcomes.

The Linux runtime supplies a writable local data root and the bundled worker path. Local signed-object transport accepts an explicit public base URL for forwarded ports and LAN clients. The ingestion image imports the shared registry cache as well as its own cache, retaining the narrow runtime image while reusing already-built worker artifacts.
