# Harness market-data runtime

## Status

The runtime implements one harness-hosted HTTP composition, a shared read-only
reader, durable local/ACA ingestion and snapshot/cache reuse. Ingestion now
streams bounded typed pages through canonical external runs and file-backed
Parquet staging; finite workers consume independently claimable immutable chunks with background
request-permit prefetch and durable final metrics. Publication uses immutable
SHA-256 object paths and a renewed, fenced symbol-day manifest. CPU, wire/decode,
session, scratch and publication resources have explicit process budgets.

The [publication release record](../verification/publication-release-2026-10-17.md)
and [earlier rollout](../verification/ingestion-fleet-rollout-2026-10-15.md) preserve
historical deployment and performance evidence.

## Boundaries

| Capability | Owner | Responsibility |
| --- | --- | --- |
| Market-data reading | `src/harness/market-reader` | Validate pinned manifests, verify/decode Parquet, bound retained caches, and visit ordered Arrow batches. |
| Harness data acquisition adapter | `src/harness/lakeclient` | Ensure/wait policy, execution-scoped manifest pinning, signed-object transport, progress, and reference/option API calls. |
| Catalog and availability | `src/lake/catalog`, `coverage`, `query` | Classify committed data and plan availability. No provider work occurs during reads. |
| HTTP | `src/lake/http` | Authenticated catalog, admission, snapshot, read and reference routes, composed under the harness API's `/market-data` prefix. |
| Ingestion execution control | `src/lake/ingest-jobs` | Conditional durable admission, immutable worker specifications, lease-fenced job state, ACA execution reconciliation and aggregate worker limits. |
| Finite ingestion process | `src/lake/ingest-worker` | Own a renewable executor anchor, fetch provider secrets once, claim compatible disjoint chunks, persist progress/final metrics, settle and exit. |
| Harness execution control | `src/harness/api/src/execution` | Retain accepted input and results, reserve finite local/ACA execution capacity, claim jobs with renewable fencing and classify expired owners as interrupted. |
| Provider acquisition/publication | `src/lake/providers`, `ingestion`, `storage` | Provider paging/governance, staging, canonical Parquet writing and symbol-day publication leases. |
| Local deployment adapters | `src/lake/local` | Atomic contract files, durable job documents, kernel-fenced manifest ownership and content-pinned finite worker processes. |
| Ingestion resources and measurements | `src/lake/pipeline` | Shared CPU/wire/decode/session/scratch/publication admission, cancellation fencing, stage/pool counters and OS CPU/RSS. |
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
API bearer credentials are never sent to signed-object URLs. Market objects use
`sha256=<digest>/<logical-partition-path>` and are immutable: replacing current
coverage retains the bytes referenced by earlier snapshots, including cold reads.
Content garbage collection needs an explicit retention contract; no publication
path deletes old generations. Local signed URLs encode canonical paths once at
the HTTP transport boundary, including symbols containing percent escapes.

## Durable partitioned ingestion

Admission retains immutable requests and the accepted executor specification in
`ingestion/jobs/<job_id>.json`. Identical inputs join the retained execution;
different inputs conflict and terminal outcomes are immutable. Option selection
is one standalone operation; dataset batches may contain multiple requirements.
The API admits and observes work without fetching provider data.

The planner divides each dataset window into independently claimable disjoint
calendar chunks: at most the configured 31-day cap and approximately four chunks
per available worker for shorter plans. Bar-only ranges coalesce into one chunk.
Starts favor roots with fewer occupied executors. A warm worker claims the next
unstarted chunk with the identical accepted worker specification, alternating
roots when possible. It shares provider connections, secrets, governor and
startup across chunks; it cannot steal running or ambiguously dispatched work.
The chunk keeps the original `job_id` and a request-list hash as its internal
partition key. State lives at `ingestion/partitions/<job_id>/<key>.json`; these
are work addresses, never additional execution job IDs. Each dispatched chunk
retains its physical executor's anchor reference. The anchor remains owned and
running until that process drains its work, so cleanup cannot stop a live reused
executor. Global admission counts unique executors, including startup, rather
than chunks. Root and chunk records precede external starts. Accepted requests
and worker specifications never change.

A renewable dispatcher lease bounds the aggregate fleet across all API replicas
and the scheduled coordinator. Existing partition records are read with bounded
storage concurrency; conditional creation happens only for absent work. Workers
own separate partition leases, so a long plan can execute in several containers
without sharing a mutable progress document. The coordinator aggregates session
progress, failures, completion and cleanup onto the original job. Each worker
rechecks committed coverage before executing its accepted partition.

Azure uses manual ACA Job executions of `<quant-api-name>-ingest`; local mode
uses content-pinned finite child processes through the same ports. Dispatch
intent precedes the non-idempotent ACA start POST. Available slots start with bounded concurrent dispatch; each start retains its own intent before I/O. A lost acknowledgement is
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
delayed responses cannot extend grants. A background refill task fetches five-second grants before the cache empties. Its short cache mutex never covers storage I/O or sleep. Workers adapt permit batches to
their observed consumption, shrinking after unused expiry and growing only when
exhausted before half their lifetime. This avoids wasting allowance on slow
workers without refunding reservations. Unused,
expired and ambiguously acknowledged grants remain charged through sixty seconds
after their last permitted use; restart never refunds possible provider requests.
Budget errors fail closed. There is no fixed division by maximum worker count,
so idle slots do not strand another worker's budget. Local and Azure have
independent authorities and must not ingest simultaneously with the same Alpaca
allowance; this is the operator's current execution policy.

Providers expose bounded typed page streams; transport owns bounded decompressed
bytes, while the Alpaca adapter decodes borrowed wire fields directly, preserving
exact decimal tokens and parsing each timestamp once. Short normalized text uses
inline storage. Ingestion owns validation, canonical per-page ordering, Arrow
projection and bounded external merging. Independent nonoverlapping runs bypass
per-row heap merging. Storage incrementally encodes canonical batches into owned
scratch files with fixed 131,072-row Parquet groups, hashes files with bounded
buffers, and streams them to local/Azure object transport. No whole-session record
array, whole-object byte vector, or provider-specific storage decoder is required.

`lake-pipeline` owns process admission, cancellation and measurements. Wire bodies
reserve their configured maximum before request allowance. A separate decode pool
reserves headroom based on actual body length plus bounded row bookkeeping and
retains it through acquisition consumption. Separating these pools prevents
queued bodies from holding all memory needed for decoding. CPU-heavy work shares
one CPU budget; publication waits and uploads use a separate I/O budget. Session
reservations cover bounded merge/encoding work, not a maximum number of daily
records. Scratch files own byte reservations until deletion, seal and close write
handles before merging, and fail visibly on capacity exhaustion. No session waits
for scratch credits held by its own files. Local admission snapshots 75% of
available host RAM, caps fleet size to its available budget, and retains that
budget in accepted execution input; an explicit host budget remains configurable.
These are explicit accounting/algorithmic bounds, not a general RSS allocator.

`PublicationCatalog` is the only production coverage implementation. Its authority
is `publications/asset_class=<asset>/symbol=<encoded>/date=<day>.json`, holding all
dataset/interval rows for that symbol-day. Readers consult committed manifests;
Azure Tables retain separate contracts/listings/job capabilities, not coverage.
Discovery filters symbol/date paths before reading manifest payloads. Azure batches
those reads with 32-way network fan-out and ordered results; local files use the
same batch port without per-file remote waits.
The publisher validates all candidate objects against their hashes and coverage,
acquires a publication slot and the manifest lease, reads the current manifest,
uploads immutable objects, merges selected rows, and replaces the manifest once.
Objects left by failed uploads are unreachable; no partial dataset set is visible.
Acquisition conflicts alone may wait up to 75 seconds. Mutations and ambiguous
acknowledgements are not replayed: a failed final acknowledgement can mean the
manifest committed, and coverage is the reconciliation authority.

Azure leases protect the manifest blob itself. The lease ID accompanies the final
PUT, so expiry/reacquisition rejects stale writers atomically. Renewal runs at one
third of the lease duration and stops before release. Local kernel ownership has
no expiry and protects atomic file replacement. Cancellation serializes with the
final visibility mutation before job settlement; aborting a caller cannot authorize
its surviving blocking encoder to publish later. Bar range acquisition remains
batched and publication remains one symbol-day at a time.

Workers emit `ingestion_metrics` every ten seconds and attach the final snapshot
to `worker_settled` and the durable terminal partition record, attributed by retained job ID and partition. Metrics include
secret setup/coverage/progress/network/decode/normalize/validate/sort/Arrow/Parquet/
hash/spill/upload/manifest/lease and
rate-cache, ledger-RPC, CAS-conflict, capacity and pacing timings, bytes/rows, pool occupancy/high-water/waits and OS CPU/RSS.
Timings are overlapping stage service time, not additive wall time. Network bytes
are decoded HTTP body bytes, not compressed bytes on the wire. The maintained
`ingestion/examples/replay.rs` exercises the full loopback HTTP-to-publication path
with synthetic quotes/trades, no credentials and isolated temporary objects; it
verifies row counts and reports throughput. It does not measure Alpaca latency,
Azure egress or ACA startup. See `docs/ingestion-performance.md` for commands.

Closed crypto days use half-open tick membership
`[open, close)` so adjacent days cannot duplicate a midnight event; an unfinished
day includes its current query endpoint when that endpoint precedes the close.

## Deployment and cutover

The combined quant API hosts `/market-data` at 1 vCPU / 2 GiB.
API activation follows both worker Job template updates; scheduled dispatchers
also depend on their worker templates. A newly active API cannot dispatch into
an older template during deployment. When a push changes both products, the
Thesis workflow waits for that commit's main deployment outside the shared
deployment lock before reapplying shared infrastructure with its retained
harness images. The main release classifies changes since the last successful push deployment,
so failed or cancelled intermediate releases cannot cause reuse of an older
runtime image with a newer contract. Azure's finite
ingestion Job uses the narrow worker image at 4 vCPU / 8 GiB on Consumption,
with platform replica retries disabled. Defaults are eight concurrent worker
executions, two sessions per worker, 31-day partitions and an 86,400-second
execution deadline. Bicep exposes `ingestionMaxWorkers`,
`ingestionSessionConcurrency`, `ingestionPartitionDays`, `ingestionCpu`,
`ingestionMemoryGiB`, `ingestionTimeoutSeconds`, `ingestionImageReference` and
`ingestionPipelineLimits` (CPU slots, response/session bytes, publication slots).
Runtime controls are the corresponding `LAKE_INGEST_*` settings; the job name,
worker identity, image, resource group and provider Key Vault are explicit inputs.
The partition default amortizes worker startup over month/year requests. Smaller
partitions favor short-plan fan-out; larger partitions reduce startup waves for
bulk history. Changing the setting affects new admissions only.

Backtest, study and screening submissions use a separate finite worker pool.
`harness-worker` runs as local child processes or the manual ACA backtest Job
(4 vCPU / 8 GiB, two workers by default); the API executes no JIT constructors or
simulations. Jobs and artifact inventories live under `execution/jobs/` in the
shared results backend. Immutable file objects publish before the owner-fenced
terminal update. Startup reservations count cold boots, are consumed once and
expire after ten minutes; attached fleet/job ownership renews every ten seconds
with a sixty-second expiry. Recovery marks lost owners interrupted and never
replays them. A 0.25-vCPU / 0.5-GiB scheduled execution dispatcher recovers and
notifies queued work even while the API is scaled to zero. Local API recovery
performs the same control work. Notification loss does not lose durable work.

Synchronous control operations run behind bounded blocking admission; async
health probes remain independent. Validation runs in a memory/deadline-bounded
compiler child while complete validation results retain their existing cache.
Result inventory and status reads never hydrate all artifacts; raw downloads
stream with bounded buffers and validate byte counts/checksums. Reading,
simulation, artifact publication and interruption are distinct durable stages.

Replay uses a chronological merge of independently admitted source cursors,
without a whole-window tick vector or event-bus queue. Preflight scans exact
fingerprints once and reopens the same pinned snapshot. Exact valuation and
ledger histories spill checksummed compressed frames; financial metrics and
Parquet consume every row. Only report visualization points are sampled.
Memory is bounded by active cursors/batches and history frames, rather than the
length of the tick window. Explicit cursor, tie-group and scratch capacities
fail visibly; they do not truncate input.

The pooled-ingestion contract requires an explicit nullable `executor` field.
Its cutover deletes only obsolete, drained ingestion job/partition coordination
documents, after stopping old API/dispatcher writers. Preserve all published
market objects, publication manifests, references, contracts and request-budget
charges. Existing harness filesystem execution metadata is obsolete when moving
to the new shared `execution/` authority; it is not silently reinterpreted.

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
executor decoder or runtime migration. The current publication cutover also
retires per-dataset Table/file coverage rows and mutable market object names;
Parquet payload schemas and canonical bytes remain unchanged. Drain accepted work,
stop API/dispatcher/worker writers, then reset affected development market objects,
coverage rows, saved snapshots and retained ingestion job/partition/executor
records before starting one matching API/worker image
pair. Preserve unrelated contracts, listings, reference data and harness state.
Preserve the charged request-allowance ledger until its outstanding window has
expired. There is no mixed-writer window, old coverage reader, or runtime conversion.
Local reset remains an explicit marker-protected operation; reads never delete
or upgrade outdated data. The October 17 release completed this cutover before
starting matching writers; its release record distinguishes the reset inventory
from fresh post-cutover smoke data.

Resetting admission IDs also requires clearing their vendor execution history.
Deleting only Blob job/partition documents leaves correlated ACA executions and
must still trip image/ownership checks when IDs are reused. For a bounded
development reset, drain every execution, hold exclusive dispatch ownership and
recreate the manual ingestion Job with its exact image, identity and resources
to clear ACA history. Release dispatch only after the definition is restored.
Never relax acknowledgement validation or add an old-image exception.

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

The query crate pins the DuckDB wrapper/native engine pair exactly, with the
same versions and native build dependencies in both workspace lockfiles. Shared
verification targets can reuse those native artifacts across workspaces. The
production image's finite lake targets omit DuckDB; the harness dependency cook
builds it once.

Azure object enumeration filters hierarchical-storage directory entries at the
SDK adapter boundary. Catalogs see file keys consistently with local storage;
directory placeholders must never be decoded as publication manifests.

The harness dependency cook is an immutable `harness-deps` stage. Runtime Cargo
files come from that stage; Rustup comes from the completed harness build, which
has installed every component requested by the managed-workspace pin. The
minimal base toolchain alone is insufficient. An offline build step probes that
pin with the unprivileged runtime user and read-only toolchain. Linked payload
copies keep unrelated image layers reusable when an earlier payload changes.

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

Finite local workers own scratch directories under `ingestion/scratch/<work-key>/`. Process cleanup removes that checked directory after the worker exits, including orphaned files from an interrupted process; system temporary storage is not left with unowned ingestion runs.

Local object replacement stages under a reserved `.fca-staging` directory on the same filesystem. That directory is outside the logical object namespace and cannot be addressed through storage ports; listings expose committed names only. Publication readers retain strict manifest-path validation.
