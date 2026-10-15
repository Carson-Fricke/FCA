# Harness market-data runtime

## Status

The source implements one harness-hosted HTTP composition, a shared read-only
reader, durable local/ACI ingestion, native catalog access, and snapshot/cache reuse. Azure rollout is a
separate release operation; source verification is not proof of deployment.

## Boundaries

| Capability | Owner | Responsibility |
| --- | --- | --- |
| Market-data reading | `src/harness/market-reader` | Validate pinned manifests, verify/decode Parquet, bound retained caches, and visit ordered Arrow batches. |
| Harness data acquisition adapter | `src/harness/lakeclient` | Ensure/wait policy, execution-scoped manifest pinning, signed-object transport, progress, and reference/option API calls. |
| Catalog and availability | `src/lake/catalog`, `coverage`, `query` | Classify committed data and plan availability. No provider work occurs during reads. |
| HTTP | `src/lake/http` | Authenticated catalog, admission, snapshot, read and reference routes, composed under the harness API's `/market-data` prefix. |
| Ingestion execution control | `src/lake/ingest-jobs` | Conditional durable admission, immutable worker specifications, lease-fenced job state, ACI reconciliation and aggregate worker limits. |
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

## Durable ingestion

`EnsureSubmission` retains immutable requests; `EnsureJob` is the worker's fresh
gap plan. The admission port never accepts mutable progress/gap selections.
Admission atomically creates `ingestion/jobs/<job_id>.json` with immutable work
and a digest-pinned worker image, destination, identity, resource allocation,
timeout and non-secret environment. Identical requests join the retained ID;
different requests conflict. The document is the job authority, rather than a
second Table projection. Terminal outcomes are immutable.

Ensure returns satisfied coverage or immediate queued acceptance. The acceptance
`Location` resolves relative to the admission route, preserving the host's
`/market-data` mount prefix. An optional
explicit `job_id` on ensure and option resolution permits an intentional fresh
attempt without overwriting a failed job. Ordinary repeated requests join the
canonical ID. Callers retain IDs and own waiting; option selection may return
202 while its provider acquisition runs externally.

Every API replica may reconcile, but one renewable dispatcher lease controls
admission to the shared worker budget. Dispatch intent is saved before an ACI
mutation. Group names derive from retained IDs. Ambiguous starts are inspected
at that resource identity, never replayed under a replacement ID. Lifecycle metadata
is written atomically with each authoritative document; listing it avoids downloading
completed history and limits queued-document reads to available worker slots. Worker leases
fence progress and settlement. After a process restart, workers recalculate
remaining gaps from committed coverage. A missing group for already-running
work becomes a visible failure rather than an automatic replay.

ACI deletion is asynchronous: a group occupies its slot until Azure confirms
its absence. Terminal groups are removed. Worker and reconciler deadlines bound
execution; failed/lost ownership stops settlement. Provider request and in-flight
ceilings are divided across the configured worker count. Old worker budgets
drain conservatively when deployment configuration changes.

## Deployment and cutover

The combined quant API hosts `/market-data`; the standalone lake API and old
scheduled Container Apps ingestion job are retired through `deploy-main.yml`.
There is no inline API-owned ingestion or standalone local lake service. Local mode runs the same finite worker against filesystem ports; Azure mode dispatches ACI.
The unused Azure Queue worker, option queue payloads and enqueue interfaces
are removed; Blob-backed admission is the only ingestion execution controller.
`lake local reset --yes` remains explicit cleanup of an owned development tree.

The dedicated ingestion image contains the worker and non-secret market-lake
configuration, without the JIT toolchain or DuckDB. ACI pulls it using the
ingester identity and retrieves provider secrets directly from Key Vault.
The combined host's cargo-chef recipe includes manifests from both Rust
workspaces, with the harness lockfile authoritative. All local crates are
skeletonized together during dependency cooking, then restored from current
sources. This keeps the cross-workspace reader dependency buildable and allows
source-only changes to reuse native dependency layers.
The dispatcher has Container Instances administration in its resource group and
identity-assignment permission scoped to the ingester identity. Updating the
conditioned GitHub deployer's role allow-list requires the repository's authorized
OIDC bootstrap before the first deployment with these assignments.
The subscription must also have `Microsoft.ContainerInstance` registered;
the release checks this before retiring workloads. Subscription provider
registration stays an operator responsibility, outside the deployer grant.

Defaults are 4 vCPU, 8 GiB, two concurrent groups and an 86,400-second timeout.
Bicep exposes `ingestionImageReference`, `ingestionCpu`, `ingestionMemoryGb`,
`ingestionMaxWorkers` and `ingestionTimeoutSeconds`. Runtime equivalents are
`LAKE_INGEST_IMAGE`, `LAKE_INGEST_CPU`, `LAKE_INGEST_MEMORY_GB`,
`LAKE_INGEST_MAX_WORKERS` and `LAKE_INGEST_TIMEOUT_SECONDS`; destination,
identity, registry and Key Vault settings are explicit deployment inputs.
Larger ACI allocations depend on regional quota. The quant API uses 1 vCPU and
2 GiB per replica and scales to zero. A scheduled 0.25-vCPU/0.5-GiB coordination job
runs the same dispatcher once per minute, while active APIs also reconcile for
fast dispatch. It never acquires provider data; ingestion compute runs only in
finite ACI groups. The coordination job has its own small execution cost.

Existing canonical Parquet and coverage records are retained. Readers reject
outdated metadata rather than reconstruct checksums. The new job authority uses
a fresh prefix; retired API process-owned job records are not interpreted or
upgraded. The cutover stops retired writers before deploying the new host.

`HARNESS_MARKET_DATA_MODE` selects the complete deployment: `local` defaults to filesystem adapters and finite local processes; `azure` requires explicit Azure storage/identity/ACI inputs. The workstation launcher builds optimized binaries and starts the complete local harness. To use Azure, local Thesis/UI select the entire remote harness.

Native HTTP/MCP execution receives catalog/acquisition and object ports directly through `MarketAccess`. There are no self-HTTP reads, ensures, reference queries or option resolution. Separate execution containers and clients explicitly configure the remote control URL; the standalone client defaults to the local combined endpoint. `/v1/read` is removed because it had no production consumer. Remote readers obtain pinned manifests and compressed objects; local download URLs are path-scoped, expiry-bounded HMAC capabilities.

Local job and publication ownership uses kernel locks, released automatically on process death. Worker executables are pinned under the owned data root by content hash, so admitted jobs retain the accepted binary and running workers do not lock the build output. Per-record atomic catalog files isolate concurrent publications and avoid whole-catalog rewrites. Local/provider credentials never enter durable job specifications.

Azure registration and the conditioned OIDC bootstrap are completed. The first consolidated Azure revision is deployed; final-source rollout and performance verification are in progress. The provider-free coordinator is named `<quant-api-name>-dispatch`, within ACA job naming limits.

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

The Linux runtime supplies a writable local data root and the bundled worker path. Local signed-object transport accepts an explicit public base URL for forwarded ports and LAN clients. The ingestion image imports the shared build cache as well as its own cache, retaining the narrow runtime image while reusing already-built worker artifacts.
