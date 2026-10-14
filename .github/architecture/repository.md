# Repository Architecture

## Runtime and FCA extension boundary

`src/thesis_runtime/` owns the generic host for conversations, tool dispatch,
subagents, workflow execution and durable workers. `src/thesis/application/`
selects providers, persistence and FCA policy. Concrete tools and workflows live
under `src/thesis/extensions/`; research, workspace and harness submission
services remain FCA-owned. Dependencies point from FCA into the runtime.

Workflow extensions cross a versioned JSON endpoint. Recovery requires the
durable workflow version to match the current registration; version tags do not
require retention of historical implementations. Tools receive trusted context
separately from model arguments. Runtime records retain opaque namespaced
references instead of fixed FCA fields. API replicas accept durable work; fenced workers own execution
independently of replica count. The current implementation remains Python with
local FCA extensions; Rust service and MCP tool hosting remain future deployment
work. The package tree, protocols, invariants and executable verification paths
are specified in [Runtime architecture](thesis-agent-workflow-runtime.md#runtime-and-fca-extension-boundary).

## Current contracts and data cutovers

The governing policy is "Current Contracts and Data Cutovers" in
[Copilot instructions](../instructions/copilot-instructions.md#current-contracts-and-data-cutovers).
Application-owned formats and interfaces have one current contract. A change
updates producers, consumers, callers, tests, fixtures, and active documentation
together and removes superseded paths. Existing data and tests do not justify
historical decoders, aliases, inferred required fields, compatibility facades,
dual writes, or multiple implementations selected by version.

During early development, delete/reset affected obsolete data in the authorized
environment by default. Necessary preservation is a bounded one-time conversion
at cutover, outside application reads, startup, and execution; remove the
temporary converter afterward. Readers may report incompatible data as outdated
and must not upgrade, reinterpret, execute, or delete it. Coordinate producers
at cutover so outdated writers cannot recreate obsolete records. Version tags
remain useful for provenance, ABI/cache identity, and mismatch rejection.

The runtime now requires explicit conversation ownership, complete durable record
fields, tagged activity, reference-only invocation snapshots, and positive
positions for accepted input. Workflow registration holds one current implementation
per ID and rejects a mismatched durable version. The UI uses declared datum types,
canonical event fields, and execution-specific phase links; retired workflow
message cards and phase aliases are removed. Artifact readers require current
manifest fields and canonical trade rows. Lake bars coverage requires provenance;
backtest manifests always publish effective, acquired, and execution plans.
Obsolete provider facades, ownership migration helpers, authoring skills, Rust
parity aliases, and lake request/config aliases are removed.

Current recovery, fencing, idempotency, supported provider adapters, deliberately
optional fields, inline/chunked storage, and generic rendering of valid unknown
datum types remain current behavior. Historical verification records remain
immutable evidence; active plans do not override this policy.

## Individual option market data

`lake-contracts::OptionSelector` is the shared wire contract re-exported by
`harness-contracts`; that crate depends on lake value contracts, never provider
or ingestion implementations. `POST /v1/options/resolve` accepts an underlying,
call/put right, explicit UTC `as_of`, expiration or target days-to-expiration,
and strike or absolute target delta. Automatic selectors cover standard
unadjusted 100-share contracts. Resolution uses the active/inactive Alpaca
contract master plus historical quotes to establish point-in-time eligibility.
Local metadata persists in `state/option-contracts.json`; hosted metadata uses
the contract partition of the existing coverage Azure Table.

Delta is estimated by implied-volatility inversion of historical option quote
midpoints and contemporaneous underlying quotes using a European Black-Scholes
model. The explicit continuous annual rate/yield inputs default to zero. Quotes
after `as_of`, stale quotes, crossed/zero-sided quotes, and unpriceable candidates
are excluded. The nearest eligible expiry is chosen before delta distance, with
deterministic tie-breaking. Defaults allow seven calendar days of DTE tolerance,
0.10 absolute delta tolerance, and one hour of quote age. This is a recorded
selection estimate for US options, not a provider Greek or a rolling subscription.

The lake serves option quotes, trades, and provider minute/day OHLCV bars through
the existing ensure/catalog/read/query routes and XNYS session partitions.
Empty provider option bars become terminal empty coverage; option bars are not
derived from trades. Provider failures remain failures. Harness `data.options`
maps program input aliases to selectors, resolves each contract once at or before
replay start, acquires all declared streams by OSI symbol, and remaps callbacks
to the alias. The actual contract, multiplier, timestamps, model method, and
selector assumptions remain in input provenance. Existing quote/trade/bar SDK
callbacks and Thesis execution submission carry these inputs unchanged.

This boundary supplies market data. Full chains, open interest, rolling,
exercise/assignment, option margin, and option-specific execution accounting
are not supplied by this interface.

## Status

SDK `strategy-sdk/src/research/` owns declarative exhibit contracts, typed
publication helpers and reusable compositions. JIT validates complete output
bindings at stop. UI `lib/research/` owns bounded immutable evidence loading and
registered panel rendering shared by chat cards and workspace inspection.
The synthetic gallery is generated from SDK output through the production
Python paging projection; it is not a second research execution path.

Workspace strategy sketches live in `workspaces/sketch_contract.py` (pure
contracts) and `workspaces/sketches.py` (Git revisions). Runtime sketch and
exploration tools use normal session dispatch. `workspaces/exploration.py`
projects immutable job output pages for tools and HTTP. The SDK owns reusable
building blocks and output contracts without depending on backtest. Canonical
multi-timeframe aggregation belongs to harness execution and serves screening.

Local harness MCP study, backtest, and screening workers durably transition
their admitted job from `queued` to `running` before acquisition, compilation,
or execution. The shared `JobStore.mark_running` operation retains accepted
input and identity, preserves the first start timestamp on repeated updates,
and rejects terminal jobs. Acceptance receipts remain `queued`; subsequent
status queries distinguish an active worker from work awaiting dispatch.

Studies, strategies and SDK-authored indicators share `MarketConsumer` market
callbacks (`on_quote`, `on_trade`, `on_bar`) and exact `Subscription` declarations.
SDK ABI v5 exposes compiled subscription unions before acquisition. Nested
program-owned indicators contribute requirements and update dependency-first;
each consumer receives only its subscribed symbols/types. Studies/indicators
use observation-only contexts; strategies receive an order sink. Bars retain
atomic multi-symbol frames and interval-end availability. Runs retain each declared bar interval and route completed frames by availability.

Thesis authors `harnessConfiguration.subscriptions`, validates the echo against
compiled source and indicator dependencies through `validate_study`, and passes
it unchanged to the study configuration. Tick-only studies require no global
interval. Studies ensure/read exactly the declared union. Backtests always add
quotes and trades for every input symbol for `simple_touch` execution, recorded
separately in the manifest without expanding consumer callbacks or the echo.
The local harness defaults to the Azure development lake; `LAKE_API_URL`, then
`LAKE_API_BASE_URL`, override the endpoint, including the loopback lake on port
8002. The stack launcher loads the Azure bearer from Key Vault into the harness
child only; the local lake retains its separate loopback bearer.
Mixed ticks and completed frames use one causal availability-ordered queue.
The older native built-in indicator registry remains separate; the shared
interface applies to authored SDK indicators composed into programs.

Thesis root, child, tool-enabled, and model-only turns share `Conversations.run_node_turn` and
`AgentSession`, the neutral `messages.decode_transcript` decoder, and the
outgoing-only `_persist_turn` writer. Incoming tool and runtime-check results use
the injected inbox persistence port and canonical incoming writer.
`SessionReplyProvider`, `reply_for`, and
the legacy executor path are removed; model-only composition uses the same
session with an empty tool registry. Durable completion mode, return anchor, and
phase-executor authority are independent of input kind. Runtime activity is a
discriminated contract that rejects untagged historical metadata. Per-conversation
input positions and `ChatThread.input_consumed_positions` fence delayed wakes;
bounded recent-ID memory and durable high-water callbacks allow idle worker
retirement. Production scheduling uses durable runnable references and renewable fenced
worker claims; API replicas hold no execution authority. Nested input uses anchored admission. Model adapters
normalize concrete HTTP/SSL errors.

Option 1's modules exist and remain the REQUIRED delivery boundary:
`thesis_runtime.contracts.messages` owns neutral `SessionMessage`/`MessageContent` contracts,
`thesis_runtime.conversations.inbox` owns the singular `SessionInbox`, and
`thesis_runtime.adapters.providers.session_adapter` owns pure live/replay projection.
`Conversations.deliver_session_message` and its handle delegate converge on
`thesis_runtime.conversations.incoming.write_incoming` for every incoming producer.
`DeliveryService` wrappers use that same writer; live tool results enter
`SessionInbox.deliver` through the injected persistence port. Directions are
`incoming`, `outgoing`, and trusted-runtime-only `instruction`, not native roles.
Session/inbox execution MUST
NOT construct provider-role histories; the adapter MUST NOT depend on runtime
services or own persistence, scheduling, ACKs, or tool execution. Immutable chat
record/activity decoding is permitted; there is no native-role reconstruction
or old delivery-schema decoder. A record without canonical session data is not
decoded. Pure
`NativeSessionAdapter.request_snapshot`/`project_native_messages` prepare one
live/replay projection. The session stores opaque `provider_request` audit
messages, exact tool catalog/options/version, hashes, and reference-only
`request_messages` in a `STARTED` invocation before execution, preserving them on
terminal updates. Ordered references contain only `message_id`, `direction`, and
`position`; canonical bodies remain in the neutral transcript. Historical full
canonical audit formats are rejected; audit references have one exact shape. Cosmos forces the
actual opaque request into workspace-scoped immutable content-addressed chunks
before saving the invocation manifest, then verifies and hydrates the complete
request on reads. Chunk/audit
write failure prevents provider execution. Native audit fields permit inspection,
not runtime role mapping
or another message path. Exact scoped correlation never scans prose.

One tool outcome has one incoming receipt with a datum array; UI expansion is
pure. ACK follows successful request/output persistence and covers only that
pending snapshot, never late mid-model inputs or results after a terminal request.
Steering persists via the canonical factory with `schedule=False`, installs
intent-epoch effects, then wakes through the ready callback. Root/model-only
execution, node/source completion, store CAS, effects outside the acceptance
guard, and deterministic-failure semantics are unchanged. See runtime `REQ-DEL`
and `../instructions/agent-session-delivery.instructions.md` for mandatory source
guard and behavior verification. Missing modules MUST fail without optional
bypasses; instructions do not guarantee LLM compliance/honesty, and module
presence does not prove complete recovery coverage.

The current hypothesis-development topology is runtime contract version `5.0.0`: discovery,
model creation, deterministic falsification trials, verdict, and the
strategy/backtest/RCA/optimization/null-testing/variation/forward-validation
chain, with one-shot agent strategy backtesting, a deterministic
signal-destruction transform in the harness, and executor-free terminal outcomes.
Unsupported historical topology is explicitly unavailable, not replaced with
today's edges. Retired persisted phase values are outside the current contract
and must be rejected, not mapped to current phases.

Workflow session policy lives behind FCA ports in `thesis/extensions/workflows/bindings.py` and
the concrete `hypothesis_development/session_services.py`, not serving. Spec-only
bindings remain available without the harness, but deny phase completion without
an execution runtime. One phase declaration derives topology, policy, and pipes;
domain live/replay transitions share the pure `fold_transition` reducer and apply
each pipe once. Generic control is reconstructed by `graph/control.py`. Acceptance checks epoch and current executor/attempt, commits with
execution-store revision CAS, projects the active frontier, and closes its executor under
the shared steering/cancellation/job guard. Events and successor entry run outside
that guard. Deterministic executor failures fail the workflow.
Harness submissions allocate one `job_id` that is durably registered with
immutable program attribution before dispatch, distinguish confirmed absence
from unavailable status, and poll with a monotonic deadline and cancellation;
recovery reconciles the retained id instead of replaying. See the "Execution
Job Contract" in `.github/instructions/copilot-instructions.md`.

Cosmos keeps one authoritative conversation store in the workspace-partitioned
catalog: records, messages, events, and per-chat sequence counters share the
container, and a message or event create commits the counter CAS and the item
in one transactional batch. Messages and immutable first-save execution
snapshots are the same documents the timeline reads; there is no projection
outbox, repair scan, or second container. Timeline readers wake on an in-process
post-commit notification and resume from their durable sequence cursor. The UI
has one `TimelineController` owner for history/live ordering and thread indexes;
scope controllers own navigation and capabilities, not duplicate transcript
caches or execution SSE subscriptions.

Root and `/api` routes are registrations on the same FastAPI app and resolve the
same app-state services. Factory defaults are fresh and composed once, never
lazily by requests; production disables in-memory defaults and missing services
return explicit 503 errors. `thesis.http.schema` exports OpenAPI 3.1 enriched by
source-derived tree/message/activity/tool schemas. The UI generates wire types
and JSON Schema from that export and validates HTTP, SSE, and offline timeline
records through one AJV registry before adapting them for presentation. Shared
chat-host and navigation controllers serve the full chat and copilot.

This document describes the target repository and deployment boundaries. The
initial skeleton, market-lake process package, shared image, foundational
storage, identity, and Container Apps modules, and the market-lake
resource-group infrastructure entry point are implemented. Versioned lake
configuration, partition/schema/calendar contracts, and reusable image
publication are also implemented. The quant-harness core records, event
kernel, OMS, strategy contract with CI-gated sandbox lint and the structural
look-ahead boundary, venues,
fees, portfolio, risk, bars/scheduler, bounded lake replay, the deterministic
backtest runner with reproducibility manifest, the streaming run recorder, the
backtest results store and its authoritative job store (one `JobRecord` per
`job_id`, artifact payloads under the `jobs/` object prefix), standard metrics
and attribution,
the point-in-time option contract universe, the capability-scoped strategy
boundary (read-only clock, opaque StrategyHistory data capability, one-way
venue commands, provenance-restricted scheduling), live-feed gap healing, the
SecurityMaster listings view, the PaperShell shared paper simulation runtime,
and the local `backtest-job` CLI are implemented; offline reports, the harness
query API, shadow/live runtimes, and CI/deployment orchestration remain
planned. The signal kernels (MODWT with circular/reflection boundaries,
additive MRA, wavelet variance, Hilbert envelope/phase features, and an
exploratory Morlet CWT scalogram) are implemented natively in the `backtest`
crate under `backtest::signal`, ported from the retired `harness.signal`
module. The retired `native` C-ABI crate and its removed Python loader have no product consumers and are deleted. Active replay, accounting, matching, and result computation live in `backtest`; authored programs cross the separate SDK/JIT ABI.

The Rust migration is complete. The engine workspace lives under
`src/harness/` and provides the authoritative engine core (clock, event
bus, replay, matching/fills, fees, portfolio, risk, results, and the
backtest runtime) plus the `harness`, `api`, `mcp`, `backtest-job`, and
`screen-job` processes. The Python engine and the embedded-Python authoring surface have
been deleted: strategies, indicators, studies, and screeners are authored in
Rust against the `harness-strategy-sdk` crate and are JIT compiled and loaded
through `harness-strategy-jit` when a configuration runs (see
`rust-native-authoring-plan.md` and `screener-rust-migration.md`). The
interpreter is no longer a harness build or test dependency. The Python engine
paths were removed in the final decommission wave, and the Python authoring
surface (host adapter, `py` extension, and the harness Python packages) was
removed in the native-authoring deletion; the `trader` binary now runs the
native Rust supervision runner (see below).

## Product Boundaries

Thesis includes its shared reference corpus as package data under
`src/thesis/knowledge/library/`; the retired repository-root `knowledge/`
location is no longer used. Workspace repositories scaffold their own
`knowledge/` alongside models and strategies. `thesis.knowledge.service`
resolves local or Gitea-backed workspace checkouts through the repository
provider and serves both sources through read-only model tools. Content
snapshots require no `.git` directory in the deployed image.

| Boundary | Path | Responsibility |
| --- | --- | --- |
| `market-lake` | `src/lake` | The lake service (Rust workspace) and the retained Python library surface: provider ingestion, stable lake schemas, storage and catalog access, coverage planning, and the lake API/client surface. |
| `quant-harness` | `src/harness` | Deterministic simulation, OMS and portfolio behavior, results, paper and shadow runtimes, and capability-gated live execution. |
| `lakeclient` | `src/lakeclient` | Consumer-facing authenticated ensure, polling, file-query, and Parquet access without provider or Azure credentials. |
| `thesis` | `src/thesis` | FCA research, workspace services, concrete tools/workflows, HTTP transport and application composition. |
| `thesis_runtime` | `src/thesis_runtime` | Generic host for sessions, conversations, tools, workflows, subagents and durable execution, without FCA imports. |
| Authored programs | `config/harness/*.rs` and inline `parameters.source` | User-authored Rust strategies, indicators, studies, and screening programs compiled against the authoring SDK. |

Dependency direction is from the harness and strategies toward published
contracts, never from the lake into harness runtime code. The harness consumes
the lake only through its HTTP routes — ensure, coverage, jobs, read, and
reference — via the `harness-lakeclient` crate; it never reads lake files or
links lake storage, and the lake must remain operable without the harness.
Strategies depend on the harness contract rather than engine internals.

## Market-Lake Packages

The market-lake service is native Rust (see `## Market-Lake Rust Workspace`
below and `lake-rust-rewrite-plan.md` for the porting order and status). The
Python packages under `src/lake` are the retained library surface for code
that is not the lake service: soak and experiment scripts import the value
objects, configuration loaders, session helpers, and the Alpaca client. The
phase-6 deletion removed every service-shaped Python package the Rust
workspace replaced (local, http, ingestion, storage, query, queue, hosted,
operations, reference, tools, the coverage/catalog service machinery, and the
top-level `app`/`crypto`/`options` modules); those directories now contain
only their Rust crates.

| Package | Responsibility |
| --- | --- |
| `lake.coverage` | The retained Python session helpers only: inclusive XNYS and UTC-day sessions with the current-session flag. Coverage classification and gap planning are native Rust in the `lake-coverage` crate. |

The harness Python packages (`harness.configuration`, `harness.core`,
`harness.data`, `harness.indicators`, `harness.research`,
`harness.screening`, `harness.strategy`, `harness.testing`, `harness.cli`,
and the `strategies/` authoring tree) were deleted with the embedded
interpreter; their behavior is native Rust in the `harness` workspace crates
(`contracts`, `config`, `num`, `marketdata`, `backtest`, `live`, `api`, `mcp`,
`strategy-sdk`, `strategy-jit`, `lakeclient`). Thesis reaches the harness
exclusively through the harness-hosted MCP endpoint and HTTP API;
`thesis.screening` is import-tested to never import the lake, the lake client,
or the harness.

The lake service Python packages (`lake.local`, `lake.http`, `lake.ingestion`,
`lake.storage`, `lake.query`, `lake.queue`, `lake.hosted`, `lake.operations`,
`lake.reference`, `lake.tools`, the coverage/catalog service machinery, and
the top-level `lake.app`/`lake.crypto`/`lake.options` modules) were deleted in
phase 6; their behavior is native Rust in the `lake-*` crates, and their
directories now contain only Rust crate content.

The event ordering contract is timestamp, source sequence, fixed serialized
event-class priority, then incoming feed order. `SimClock` advances only from
explicit events; `WallClock` is an adapter boundary and is not used by the
synchronous simulation kernel. OMS status and fill-ledger changes occur only by
applying validated execution reports. Configuration hashes normalize mapping
order, exact decimals, and UTC timestamps, and reject resolved secret values.
Historical queries are hard-capped at the engine clock head: a query for data
newer than the head raises `LookaheadError` rather than silently clipping,
and the strategy-facing capability takes no timestamp parameters, so future
reads are structurally unrepresentable (supplied future window ends still
raise `LookaheadError` as defense-in-depth).

The deterministic backtest runner is the native Rust engine: it composes lake
replay, the simulated venue, fees, risk, portfolio accounting, and the fail-loud
look-ahead abort behind one engine clock, and emits a reproducibility manifest
covering configuration, code, seed, data, fill-model, fee, and risk
fingerprints. `backtest-job --config` runs it against the market-lake HTTP
routes with JIT-authored programs, streaming market data through
`harness-lakeclient` and writing canonical run artifacts under `.local/`.

The `harness`, `api`, `mcp`, `backtest-job`, and `screen-job` processes are
served by the Rust binaries directly; the Python console-script shims were
deleted with the harness Python packages. `trader` runs the native Rust
supervision runner: it loads the configured shell, resolves one execution job
id, and runs one supervised paper or shadow session over an injected market
feed and kill switch.

## Quant-Harness Rust Workspace

`src/harness/` is a Cargo workspace providing the quant-harness engine and
its interfaces, and it contains no Python: the engine, the process binaries,
and the authoring surface are all native Rust. The native engine core and the
`harness`, `api`, `mcp`, `backtest-job`, and `screen-job` processes are
authoritative. The
embedded-Python host, the `py` extension crate, and the harness Python
packages were deleted in the native-authoring wave.

```text
src/harness/
|-- Cargo.toml                 Workspace manifest (resolver 2)
|-- rust-toolchain.toml        Pinned stable toolchain
|-- num/                       Numeric kernels
|-- marketdata/                Market-data port
|-- backtest/                  Backtest, sweeps, and results port
|-- live/                      OMS, fills, venues, portfolio, risk port, and the `trader` binary
|-- api/                       CLI facade, axum HTTP boundary, api/mcp/backtest-job binaries
|-- mcp/                       JSON-RPC 2.0 / MCP server and tool catalog
|-- contracts/                 Dependency-light subscription vocabulary and configuration hashing
|-- strategy-sdk/              Authoring SDK: traits, versioned C ABI, entry-point macros
|-- strategy-jit/              JIT seam: managed workspace, cargo build, libloading, engine adapters
|-- lakeclient/                Async lake HTTP client: ensure, coverage, jobs, Arrow reads, reference
`-- config/                    Authoritative document parser, shell/replay schemas, typed normalization
```

- `api` owns the `harness`, `api`, `backtest-job`, `screen-job`, and `mcp`
  process binaries; its axum routes, bearer authentication, and provenance
  fields mirror the retired `harness.http` module. The `backtest-job` binary
  drives the native engine through the `harness-strategy-jit` seam, the
  `harness study-job` entry point runs one authored study configuration over
  subscribed lake market inputs through the same seam, and the `screen-job`
  binary runs one authored screening program through it.
- `mcp` owns the pure JSON-RPC dispatch, the capability-filtered tool
  catalog, bearer credential resolution, and newline-delimited stdio framing.
  Its injected `StudyValidator` supplies optional opaque identity and result
  metadata; MCP never inspects compiler files or runs identity probes.
- `contracts` (`harness-contracts`) owns tagged market subscriptions, data-type
  and interval vocabulary, canonical subscription normalization, and the shared
  configuration hasher. It depends on serialization/hash libraries, not the
  engine. The SDK, configuration, and engine consumers depend toward it.
- `strategy-sdk` depends on `harness-contracts` and `num`, not `backtest`, and
  exposes the authoring traits (strategy, indicator, study,
  and screening program) over its author-facing event types with the capability
  split enforced by construction: studies and SDK indicators observe subscribed
  quotes, trades, and atomic bar frames without an order surface; strategies
  receive an order sink, and a screening program sees only its `ScreenContext`
  (universe, bars, facts, and precomputed indicators). Its versioned C ABI is
  the only contract that crosses a dylib boundary, and the loader rejects
  artifacts whose ABI version differs.
- `strategy-jit` writes authored sources into isolated managed crate/build
  generations, compiles them with the pinned toolchain, caches with source and
  compiler provenance, loads the cdylib through `libloading`,
  and adapts it to the engine's `StrategyRuntime` seam. Screening programs
  bypass the engine: they run one-shot over the injected screening data source
  (the `harness-lakeclient`-backed adapter) and the host ranks the returned
  rows. Compilation output, the toolchain digest, and the source digest feed
  the bundle-hash provenance flow; a configuration's `bundle_hash` must equal
  the source digest.
  The JIT compiler owns compiler identity and cached successful probes with
  fresh configuration/executable checks. Its `ValidationService` holds at most
  64 full-input validation results, including deterministic rejections, but no
  infrastructure failures. `api::local::NativeStudyValidator` delegates to this
  service. Cargo JSON diagnostics, not regexes, establish authored compiler
  failures. Source/configuration fingerprints invalidate the actual artifact as
  well as validation reuse; Python has no validation LRU or compiler probes.
  `compile.rs` is the coordination/public facade; `compiler.rs`, `provenance.rs`,
  `diagnostics.rs`, and `validation.rs` own build execution, identity, Cargo
  diagnostics, and validation reuse. OS file locks serialize compiler/source
  builds and manifest merging across processes; atomic manifest replacement
  publishes completed generations. Returned libraries are never overwritten;
  generations are not automatically garbage-collected. This requires Rust 1.89
  or newer for standard-library file locks. Linux behavior has not been tested
  in this change. `build.rs` embeds the JIT source-tree build identity.
- `lakeclient` is the harness's only lake boundary: an async `reqwest` client
  (with a blocking facade for the JIT screening path) covering ensure,
  coverage, job status, streaming Arrow IPC reads, and the reference
  universe/facts views over the published lake routes. It replaces the
  retired `LakeEnsureClient`; there is no local lake path or data directory.
  `ensure_many` acquires symbol windows concurrently under a fixed fetch
  bound; the lake's provider lane remains the rate limit. Study and tick jobs
  then consume `visit_arrow` batches one window at a time, releasing Arrow
  batches after conversion to replay events. Replay events remain in memory.
  Arrow transfers have a 60-second connect/read inactivity limit, without a
  whole-response buffer or overall transfer deadline. `read_arrow` and
  `read_many` collect batches only for callers requiring a batch list.
- `contracts::progress` defines harness execution stages and observed dataset
  counters with a caller-injected reporting port. The shared MCP job store
  retains the latest stage/dataset snapshots, timestamps and errors under the
  admitted `job_id`, including on failure and after restart. Terminal jobs
  reject late telemetry. HTTP and local MCP study/backtest runners inject the
  same persistence adapter. Thesis's existing job-status passthrough exposes
  these observations; workflow orchestration does not own telemetry or acquire
  another submission or polling service.
- `config` owns the non-executable YAML/JSON document parser, shell models,
  replay validation, and exact decimal/time normalization used by API, MCP,
  CLI, and engine projection. `backtest` projects those validated documents into
  engine types rather than maintaining another parser or replay schema.
  Shared hashing explicitly preserves both shipped v1 encodings: submitted
  document and sweep identities use UTF-8; resolved engine configuration uses
  historical ASCII escaping. Submission and resolved identities are intentionally
  distinct, with golden coverage rather than re-pinning persisted hashes.
  The contracts interval vocabulary accepts canonical positive units; engine
  support remains a separate policy limited to `1min`/`1d` and one bars interval.
- `marketdata`, `backtest`, and `live` are engine ports with
  focused unit tests. `marketdata` path-depends on `lake-contracts` and
  re-exports its `time`, `symbol`, `AssetClass`, `DataType` (including
  `Bars`), `ExchangeSession`, `partition_path` (with the interval dimension),
  and session calendars, so the harness and the lake share one value layer;
  the duplicated harness holiday tables were deleted and the harness honors
  the lake's ad-hoc closures and early closes. `backtest` backs the flipped
  `backtest-job` and acquires exact consumer requirements for studies, adding
  quote/trade execution feeds for backtests; `live` provides the
  deterministic live core and the supervision runner the `trader` binary drives.

The workspace is gated by `cargo fmt --all --check`, `cargo clippy --workspace
--all-targets -- -D warnings`, and `cargo test --workspace`; no Python
interpreter is required.

## Market-Lake Rust Workspace

`src/lake/` is a Cargo workspace at the product root whose `Cargo.toml` and the
Python package `lake/` coexist, exactly as `src/harness/` does. The lake
service is authoritative: the `lake` binary serves the published catalog,
ensure, jobs, read, reference, query, and raw routes, and the `lake` console
script is a thin Python shim (`lake.cli`) that execs it, with binary discovery
through `LAKE_RUST_BINARY_DIR` and `src/lake/target/{release,debug}`.

The lake's data types are `trade`, `quote`, and `bars`, plus the reference
snapshots. `bars` partitions carry the `interval=1min|1d` dimension and are
fetched provider-first, falling back to derivation from stored trades; a trade
partition revision invalidates its derived bars, and provider bars replace
derived bars when available. `GET /v1/read` streams `trade`, `quote`, and
`bars` partitions as Arrow IPC, and `GET /v1/reference/universe` /
`GET /v1/reference/facts` serve the point-in-time classification universe and
facts.

```text
src/lake/
|-- Cargo.toml                 Workspace manifest (resolver 2)
|-- rust-toolchain.toml        Pinned stable toolchain
|-- contracts/                 Asset classes, data types (including bars), sessions, partitions (including the interval dimension), Arrow schemas
|-- configuration/             Strict YAML-subset loader, environment overrides, settings models
|-- catalog/                   Coverage/job/run/contract/listing records, repositories, JSON store
|-- coverage/                  Coverage classification, gap planning, ensure application
|-- providers/                 Alpaca client, paging, retry, request-budget governor
|-- storage/                   Arrow/Parquet writer, atomic object replace, capacity guard, lease ports
|-- ingestion/                 Equity and crypto ingest transactions, committer boundary
|-- query/                     Bounded file queries
|-- http/                      Axum boundary publishing the lake contract
|-- local/                     Local layout, ingest/backfill, ensure launcher, local composition
`-- cli/                       The `lake` binary
```

Crate conventions follow `.github/instructions/rust.instructions.md`: unit
tests that need private access live in `tests/unit/<name>.rs` pulled in with
`#[path]`, integration tests are `tests/<name>.rs`, and no inline
`#[cfg(test)]` modules exist. The phase-0 parity oracle lives in
`src/lake/tests/oracle/` (generator plus committed fixtures) and the Rust
oracles load it. The workspace is gated by `cargo fmt --all --check`,
`cargo clippy --workspace --all-targets -- -D warnings`, and
`cargo test --workspace` in the `lake-rust` CI job.

## Program Authoring Contract

A program is Rust source authored against `harness-strategy-sdk`, carried by
a configuration, JIT compiled on submit, and driven through the versioned C
ABI by the native engine or, for screening, by the one-shot screening runner.
The implemented contract is:

- Authoring. Studies, strategies, and SDK-authored indicators implement
  `MarketConsumer`: `subscriptions(&self) -> Vec<Subscription>` declares only
  that consumer's inputs, and `indicators(&mut self) -> Vec<&mut dyn Indicator>`
  returns references to owned dependencies, including nested indicators.
  `Subscription::Bars { symbols, interval }` accepts canonical `1d`, `Nmin` or `Nh`;
  `Subscription::Quote { symbols }` and `Subscription::Trade { symbols }` have
  no interval. Common callbacks are `on_quote`, `on_trade`, and
  `on_bar(&BarFrame, &mut Self::Context)`, with `&mut self`.
  Studies and SDK indicators use `Context = ObservationContext`; strategies
  use `Context = OrderSink`. `StudyProgram` owns `new(parameters)` and
  `on_stop(&mut MetricPublisher)` and exports with `export_study_program!`.
  `StrategyProgram` owns `new`, `on_start`, `on_fill`, and `on_stop` and exports
  with `export_strategy_program!`. `Indicator` requires symbol-aware `value(symbol)`, `sequence()` and
  `sequence_mut()` over `IndicatorSequence`. The dependency router records
  completed outputs including undefined warm-up and missing-input values.
  Tick indicators declare nanosecond window fidelity; trade/quote outputs
  retain OHLC and counts separately from the unfinished window. Native
  indicators expose `samples()`, `advance()` and `in_progress()` with the same
  causal distinction; no forced flush promotes a partial window. Requirements and dependency topology remain stable
  after construction; cycles and zero-sized indicator dependencies are rejected.
  Dependencies update before their parent, and each consumer receives only its
  own subscribed types, symbols, and interval. Bars are atomic frames, not raw
  single-bar study callbacks, with exact `Decimal` OHLCV. The native built-in
  indicator registry has not migrated to this SDK interface. A screening
  program implements `ScreeningProgram` (`new(parameters)`, `screen(ctx)`)
  and exports itself with `export_screening_program!`. One authored crate
  exports exactly one program. The SDK exposes only the capability surface
  the author is allowed: a study has no order surface by construction (the
  vtable carries no order path), mirroring the retired
  `ResearchModelProgram` contract, and a screening program sees only its
  `ScreenContext` — universe, bars, engine fact sheets, and precomputed
  indicators — with no order, file, network, or clock access.
- Submission. `strategy_instances[].implementation` must be `"jit"`. The
  authored source travels as `parameters.source` (inline, for JSON
  configurations) or as `parameters.source_path` (a file relative to the
  configuration document, resolved at load time). `bundle_hash` must equal
  the SHA-256 of the authored source; the JIT seam rejects a mismatch.
- Loading. On submit the seam writes the authored crate into the managed
  workspace, compiles it with the pinned toolchain (cached by source digest,
  program kind, SDK version, toolchain digest, and SDK ABI version; a
  stale-ABI entry is a cache miss and is rebuilt), loads the cdylib through
  `libloading`, checks the exported SDK ABI version (4) against the host's
  and rejects an artifact built against an older version, constructs the
  program state from the configured `parameters`, and drives it through the
  engine's `StrategyRuntime` trait. A panic or failure inside authored code
  is contained and reported as a strategy-execution error.
- Market acquisition and replay. ABI v4 exposes the constructed program's
  canonical effective subscription union as JSON, including recursive indicator
  requirements, before acquisition. Thesis passes
  `harnessConfiguration.subscriptions` unchanged into `data.subscriptions`;
  `validate_study` returns the compiled union and rejects a canonically unequal
  echo. A study (`shell: study`) ensures and reads exactly that union through
  lake routes. Bars ensures fetch provider bars or derive from already stored
  trades, never trigger undeclared trade acquisition. Backtests additionally
  acquire quotes and trades for every input symbol. The manifest distinguishes
  `effective_subscriptions`, `execution_subscriptions`, and their normalized
  `acquired_subscriptions` source union. Execution feeds do not expand consumer
  routing or the configuration echo; `simple_touch` never fills from bars.
  Only one distinct bar interval is supported across
  a run, including dependencies and instances; conflicts fail before lake
   ensure/read. Tick-only inputs need no interval. Interval-only configuration
   documents are rejected before any acquisition; authored source uses
   `MarketConsumer` and rebuilds for ABI v4. Mixed replay uses one causal
  availability-ordered queue: at equal time, ticks precede completed frames,
  then generated execution events. Frames become available at interval end,
  never at bar start. Study artifacts remain `study.json` and `metrics.json`.
- Signal-destruction nulls. A backtest or study configuration may carry an
  optional strict top-level `null` block (`mode: permutation | block_permutation`,
  `block_size`, `seed`). Before preflight and replay fingerprints, the harness
  permutes per-symbol bar log-return blocks with a symbol-domain-separated
  seed, rescales OHLC exactly through `checked_mul_div`, and scales
  quote/trade ticks by the enclosing bar's factor, so manifests and
  fingerprints describe exactly the transformed data the strategy saw. A
  symbol with events but no bars fails the job instead of silently no-opping.
  Ordinary permutation requires `block_size: 1`; block permutation preserves
  within-block dependence. Both move return magnitudes across timestamps, so
  neither preserves calendar volatility scheduling. Fewer than two return
  blocks and constant-return designs fail rather than report null evidence.
  Absent `null` leaves configuration hashes and goldens unchanged.
- Screening. A screening configuration declares `shell: screen` and carries
  the LF-normalized authored source and its `bundle_hash`, the `as_of` date,
  `bar_interval`, `lookback_days`, `rank_metric`, an optional
  `universe_revision`, and the Thesis `definition_id`/`definition_hash`
  provenance. `harness screen-job` / the `screen-job` binary compiles the
  source through the JIT cache, builds one `ScreenContext` (as-of universe,
  bars, engine fact sheets, and precomputed indicators) from the
  `harness-lakeclient` data source over the lake routes, runs the program
  once, and ranks rows by `rank_metric` descending with symbol ascending on
  ties. The result is written as `screen.json` (schema
  `fca-harness-screen-v2`) carrying the ranked rows, bundle hash, rank
  metric, data window, definition provenance, and the source/toolchain/job
  digests; its closed `status` is `succeeded` or `failed`, a failed run
  records its reason in the artifact, and Thesis imports v2 only. The same
  path is published as `POST /v1/screeners/jobs`, with status, results, and
  artifacts on the neutral `/v1/jobs/{job_id}` routes, and as the
  `run_screener` and `screener_config_template` MCP tools; `get_job_status`,
  `get_job_result`, `list_artifacts`, and `read_artifact` consume the
  resulting job.
- Metrics. `MetricPublisher::publish(name, value)` accepts a finite numeric
  value under a non-reserved stable name; every rejection is recorded
  host-side and fails the run even when the author ignores the returned
  error. Repeated publication replaces the prior value; final values are
  written to `metrics.json.custom`, the canonical run artifact, the
  recorder's `emissions.parquet`, and the local/HTTP job store. Metric
  names are run outputs, not indicator registrations.
- Guidance. The plan and status live in
  `.github/architecture/rust-native-authoring-plan.md`; the screener
  migration is specified in `.github/architecture/screener-rust-migration.md`;
  the remaining reference port and re-pinned parity expectations are the next
  phases.

## Decommission Status

The engine lives entirely in Rust under `src/harness/`;
the Python engine was deleted in the final decommission wave, and the
embedded-Python authoring surface (host adapter, `py` extension, harness
Python packages, and the `strategies/` tree) was deleted in the
native-authoring wave. The harness process binaries are served directly; no
harness build or test needs a Python interpreter.

### Met criteria

1. `backtest-job` executes the Rust engine end to end. The `api` binary
   drives `harness_strategy_jit::run_backtest_configuration` over the
   configured `strategy_instances`.
2. Authored Rust programs run through the JIT seam from loaded dylibs,
   proven by the `strategy-jit` suite (study, strategy, and screening runs,
   the lake-route adapter, capability gate, panic containment, metric
   rejection).
3. The authoring surface is decoupled from the engine. The SDK owns the
   authoring traits and the versioned C ABI; the engine consumes only the
   `StrategyRuntime` seam.
4. The harness process binaries (`harness`, `api`, `mcp`, `backtest-job`,
   `screen-job`) are the only serving surfaces; the Python console-script
   shims were deleted with no dangling harness imports.

### Trader supervision

The Rust `trader` binary composes the deterministic live core with the native
supervision runner: it loads the trader configuration, resolves one execution
job id, and runs one supervised paper or shadow session through config loading,
session resolution, feed supervision, the boot reconciler, the kill-switch
gate, and durable state snapshots. The process composition currently injects
the deterministic in-process market feed and an in-process kill switch;
composing the configured Alpaca feed and the table-backed operator kill switch
remains deployment work.

### Retained Python products

`pyproject.toml` keeps the dependencies the surviving Python products
require: the lake service shim and its retained library surface (`lake`), the
`lakeclient` product, and Thesis. No harness Python surface remains installed
or tested. The Python test suite covers the retained lake and Thesis
surfaces; the Rust suites cover the engine and the harness.

## Thesis Packages

Thesis remains a peer product. `thesis.workspaces` owns isolated workspace
identity, repository scaffolds, and committed Rust program operations;
`thesis.persistence` journals accepted commands and fences
Git publication; `thesis.auth` binds operator sessions to workspaces;
`thesis_runtime.conversations`, `thesis_runtime.execution`, and `thesis.http` define the UI-facing
workflow boundary; `thesis.agent` owns policy-driven model context and
provenance; `thesis.integrations` owns the harness HTTP and MCP adapters;
`thesis.identity` owns durable accounts, one-time invite links, and
server-side sessions for the invite-only public release (ADR-001);
`thesis.paths` owns the portable relative-path rule shared by the strategy
bundle and research model stores; `thesis.research` owns content-addressed
research models that carry an authored
Rust study program source, published under `models/<model-id>/<content-sha>/`
with each run's evidence under `models/runs/`; workspace indicator definitions
remain content-addressed under `indicators/<name>/<content-sha>/`, and the
Python `harness.indicators` registry was deleted with the native-authoring
wave. `thesis.strategies` owns
content-addressed strategy bundles and `WorkspaceStrategyArtifacts`, which
authors pure Rust workspace files and projects published versions for the
Strategies HTTP view. `strategies/contracts.py` owns the shared design schema;
the context-bound `thesis/extensions/tools/strategy.py` adapter accepts no
model-supplied workspace or execution authority. Generic workflow `phase_tools`
grants remain independent of the concrete strategy capability. Fenced Git
publication commits exact accepted artifact bytes through a temporary index,
excluding unrelated staged and dirty files. Completion verifies the persisted
receipt; structural checks are not compilation or a security sandbox.
`thesis.artifacts`
owns reproducibility-critical result imports; `thesis.backtests` imports
completed harness jobs into workspace backtest records (equity, trades, stats)
linked to the originating chat and workflow execution; `thesis.screening` owns
content-addressed screener definitions and imports harness screening runs as
workspace results — it launches `run_screener` through the harness MCP and
never reads market data; and `thesis_runtime.adapters.providers`
keeps model credentials behind backend-only protocols.
Workspace identity remains `.thesis/workspace.yaml`; publication state is
written separately to `.thesis/publication.json`.

The current program-bound workflow uses
`thesis.workspaces.programs.WorkspacePrograms` and `thesis/extensions/tools/program.py`:
`studies/<program_id>/`, `strategies/<program_id>/`, and `indicators/<program_id>/`
contain `program.rs`, `manifest.json`, and a committed `validation.json` receipt
after validation. Latest committed source/configuration is authoritative for
ID-based reads, validation, and execution; dirty files are never substituted.
Targeted updates require the current content version and one exact source match.
Versions hash source/manifest digests including transitive indicator dependencies,
not HEAD. Pinned loads assemble every dependency from the same reachable commit.
Harness compilation validates the actual committed program; runs require accepted
validation matching content and SDK identity. Authoring commits exact files through
a temporary index and refuses artifact-local staged/dirty changes. Publication
failure retains the known local commit for explicit reconciliation, never automatic
re-authoring or execution replay. The legacy strategy-domain service and Strategies
HTTP projection above are separate from this workflow authoring path.

Workflow completion carries compact program references rather than inline Rust.
`hypothesis_development/session_services.py` checks scope, pinned/latest content,
the latest stored validation's source commit, file hashes, and SDK identity without
compiling or repairing source. `workflows/research_models.py::ProgramSnapshots`
is the read port for accepted snapshots and dependency assembly; later edits do not
change accepted source. Program tools use trusted context and the existing
registry/dispatcher/inbox and include ID listing beside read/validate/run,
replacing conflicting raw harness/legacy author tools.
`read_sdk_reference` serves bounded matching SDK source, including the `decimal`
kernel topic, and templates;
`template=basic` renders exact subscriptions. Local SDK sources are required even
with a remote harness. Wheels and source distributions package canonical
`harness/strategy-sdk`, `harness/contracts`, `harness/num`, and `lake/contracts` Cargo manifests and
Rust sources with portable relative path dependencies. SDK version/ABI metadata
comes from the packaged SDK; SDK identity hashes all four crates' manifests
and source files. Shared minimal templates in `workspaces/programs.py` back both
reference reads and basic authoring, not completed research implementations.
Shared workflow prompt guidance owns targeted repair and
asking after three unsuccessful compiler repairs, not a runtime repair cap.

Workspace state exposes strategy lineages, evidence, and consent/audit records.
`thesis.http.artifacts` adds a read-only durable artifact catalog (committed
strategies, studies, and indicators) with a bounded UTF-8 file reader behind the
Workspace artifacts page; volatile stores are excluded. Planning projections and
external task-tracker initialization are not part of workspace provisioning,
knowledge exports, or the UI.

The implemented runtime modules and facades are:

| Surface | Responsibility |
| --- | --- |
| `thesis_runtime.contracts.messages` | Immutable neutral session/content/request/correlation contracts and canonical-only transcript decoding. No execution or provider projection. |
| `thesis_runtime` | Public conversation, child, session, lifecycle, control, delivery, and store facade. `conversation.py` composes admission, execution, and outgoing persistence; `incoming.py` is the singular incoming writer; `inbox.py` owns injected delivery and success-bound consumption; `turns.py` owns locked worker intake. `session_reply.py` is removed. |
| `thesis_runtime.contracts.activity` | Immutable discriminated runtime activity (anchor, stage, timing, delivery key), generated metadata/message schemas, and canonical activity decoding. Tool requests and correlations live only in the canonical session message. |
| `thesis_runtime.adapters.providers.session_adapter` | Pure `project_native_messages` and versioned `NativeSessionAdapter.request_snapshot` shared by live/replay; completion calls only injected `provider.complete` and returns neutral output. |
| `thesis.http.schema`, `wire`, `errors` | Source-derived OpenAPI/runtime schemas, typed response envelopes, and explicit error/service-resolution boundaries. |
| `thesis.application.workflows` | FCA workflow composition and session binding. |
| `thesis.extensions.workflows.bindings` | FCA `WorkflowSessionServices`, `WorkflowSessionBindings` and `RuntimeToolActivity` contracts. |
| `thesis.extensions.workflows.session_services` | Routes FCA workflow capabilities by durable execution identity and grants discovery a scoped child launcher/reader. |
| `thesis.extensions.workflows.research_investigation` | Model-backed proposal, critique, adjudication and constructive training investigation branches, concurrent discovery delegation, retained child jobs and canonical asynchronous findings. Uses the generic host without importing another concrete workflow. |
| `thesis.extensions.workflows.hypothesis_development` | Concrete workflow facade over spec/doctrine, phases, contracts, prompts, services, artifacts, piping, and state/rebuild; exports `fold_transition` and `rebuild_state`. |
| `hypothesis_development/session_services.py` | Role policy, scoped schema/knowledge/authoring bindings, completion validation/application, and workflow failure callback. |
| `hypothesis_development/exposure.py`, `design.py`, `provenance.py` | Submission-time exposure ledger (one durable record per job, before dispatch), the frozen research design derived from accepted development evidence, and completion provenance checks that bind each reported job to its executing run. |
| `thesis.workspaces.programs` | Committed program source/manifests, content/dependency versions, harness validation receipts, ID-based execution, pinned loads, and matching SDK references/templates. The workspace facade exports `WorkspacePrograms`, `ProgramArtifactError`, and `ProgramPublicationError`. |
| `thesis.extensions.tools.program` | Trusted-context program author/update/read/validate/run and SDK-reference bindings on the existing session registry. |
| `thesis.workflows.research_models` | `ProgramSnapshots` port and accepted study-reference resolution for deterministic research execution. |
| `thesis.knowledge` | Read/search facade; `service.py` owns source isolation and hybrid operations, `embeddings.py` owns pinned/checksummed semantic loading and coherent operation-level fallback, `library/` contains shipped data. |
| `thesis_runtime.adapters.cosmos` | One authoritative workspace-scoped conversation store: records, messages, events, per-chat sequence counters, atomic sequence commits, root tombstones, and bounded background conversation cleanup. |
| `thesis_runtime.adapters.cosmos.chunks` | Immutable bounded JSON leaves/reference pages in existing containers, content/digest-verified hydration, and forced opaque provider-request storage before invocation persistence. Unreferenced chunks are retained; no automatic collector. |
| `thesis_runtime.adapters.cosmos.stack` | Single-store chat/timeline and execution-store composition; both openers retain `ensure_database` bootstrap, creating the database and catalog container if absent. Opening is not side-effect-free. |
| `src/ui/src/lib/timeline.svelte.ts` | Single chat history/live owner with ID merge, `seq` ordering, and thread indexes. |
| `src/ui/src/lib/workflow-scope.svelte.ts` | View navigation, expansion, durable capabilities, and anchored operator submission over the injected timeline. |
| `src/ui/src/lib/chat-host.svelte.ts`, `chat-navigation.ts` | Shared chat lifecycle and URL navigation for full chat and copilot hosts. |
| `src/ui/generate-wire.mjs`, `src/ui/src/lib/wire.ts` | Generate types/schema from backend exports and validate HTTP/SSE/fixture wire records through AJV. |

Knowledge model artifacts are pinned to a commit and verified checksums. Loading
is single-flight; vector caches include backend identity. A primary inference
failure retries the whole hybrid operation once on the lexical backend, never
mixing vector spaces. UI node projection reconciles attempt/timestamp freshness
on re-entry. Workflow topology comes only from a fetched current registered
definition matching the recorded version; unavailable topology is not invented.

`thesis.workspaces.repository.WorkspaceRepositoryProvider` is the structural
local/remote boundary: create, open, confined path resolution, locator, and
publication. `LocalWorkspaceProvider` retains native local Git repositories;
`GiteaWorkspaceProvider` provisions private, ownership-marked repositories and
uses rebuildable checkout caches. Azure catalog composition injects this
provider without changing the workspace HTTP interface. Git is authoritative
for remote manifests; Blob containers retain catalog mappings and operational
metadata. Remote publication pushes existing accepted commits, never arbitrary
dirty files. `WorkspaceHypothesisStore` now uses this provider with an explicit
workspace ID for every operation. Canonical Markdown bytes publish immutably at
`hypotheses/<slug>/<sha256>.md`; `.thesis/hypotheses/<slug>.json` selects the
accepted version. The workspace journal and fenced Git publication commit exact
bytes, and loads verify hash, identity, and committed content. Loads require the
publication manifest; legacy `hypotheses/<slug>.md` has no compatibility read or
guessed migration from a shared provider root. Automatic publication of every
other artifact writer is not implied by this boundary.

`infra/modules/gitea.bicep` optionally hosts open-source Gitea on internal
Container Apps ingress with persistent Azure Files storage. It uses one replica
with a Burstable PostgreSQL database and verified TLS. SQLite on SMB failed
deployment write-locking and is removed. The Thesis image includes
native Git; only Thesis receives the forge credential through Key Vault.

Remote deployment resolves model, harness, and forge credentials through Key
Vault. Thesis calls the quant API HTTPS ingress for MCP; the quant API receives
the lake endpoint and bearer. Environment diagnostics collect logs for all
apps. Thesis runs one always-on replica because scheduling is process-local.
`scripts/local/run_remote_ui.py` starts the existing loopback Vite proxy against
Azure, resolving the API bearer through Azure CLI without putting it in the
browser bundle. It requires Azure login and Key Vault secret read permission.
Public multi-user UI hosting still requires the separate Entra bootstrap.

## Shared Foundation

The products share repository-level mechanics where divergence would create
operational drift:

- `pyproject.toml` defines the Python project and common quality tooling.
- `src/harness/` is a Cargo workspace with a pinned stable toolchain. It
  builds the `native` C ABI core and the harness interface binaries. CI
  enforces `cargo fmt --all --check`,
  `cargo clippy --workspace --all-targets -- -D warnings`, and
  `cargo test --workspace` alongside the Python gates.
- `Dockerfile` builds one immutable image with distinct runtime commands. A
  Rust build stage compiles and tests the workspace and produces the native
  core library plus the `harness`, `api`, `backtest-job`, `mcp`, and `trader`
  binaries; the runtime stage carries the authoring SDK under `/opt/rust/sdk`
  and the binaries on `PATH` under `/opt/rust/bin`.
- `config` contains versioned, non-secret YAML grouped by product or concern.
- `infra` contains reusable Bicep modules, environment parameters, identity,
  storage, observability, and Container Apps definitions.
- `.github/workflows` provides path-filtered checks, image publication, and
  OIDC-based deployment.
- `tests/lake`, `tests/lakeclient`, and `tests/thesis` mirror their product
  capability boundaries. `tests` also holds explicit cross-product contract
  tests where integration is intentional.

## Market-Lake Deployment

`infra/main.bicep` is the resource-group entry point for the market-lake dev
slice. `infra/parameters/dev.bicepparam` selects `westus2` and the `dev`
environment. Names use the `fca-ml-dev` prefix; the storage account and Key
Vault add deterministic suffixes derived from the deployment resource group,
while the remaining resource names are stable from the prefix and environment.

The composition deploys storage, managed identities and GitHub OIDC, Key
Vault, runtime access, Container Apps, and cost controls together. The dev
parameters explicitly enable the Key Vault public endpoint because no private
endpoint module is composed yet; the vault remains Entra-only and RBAC-based.
The private GHCR image is mirrored by digest into a private Basic ACR during
deployment. Container Apps pulls the mirrored digest with the runtime managed
identity; the ACR admin account is disabled. The budget alert contact
placeholder must be replaced with a valid user-supplied email before Azure
operations. No secrets are stored in Bicep or parameter files.

The lake lifecycle policy moves blobs to Cool after 14 days of no modification.
Cold tier is deliberately disabled: retrieval and early-deletion charges make a
Cold transition a retrieval-cost decision rather than a default tiering step.
The rationale and re-evaluation trigger are recorded in
`.github/runbooks/cold-tier-policy.md` (T065).

Container Apps logs use the `PerGB2018` Log Analytics SKU with its required
30-day minimum retention. The application operations setting reports the same
effective retention guardrail.

Sharing these roots does not imply unrestricted cross-package imports. New
shared code should be introduced only when a stable contract has more than one
real consumer.

## Runtime Shape

```text
market data providers
        |
        v
market-lake ingest jobs --> Azure lake + catalog --> market-lake API/client
                                      |                       |
                                      +-----------+-----------+
                                                  v
                                quant-harness backtest jobs

live market feeds --> quant-harness paper/shadow/live runtime --> broker API
```

Market-lake APIs and ingest jobs, harness backtest jobs, and harness trading
runtimes are separate commands from the shared image. Azure resources use
managed identity and least-privilege access. GitHub Actions uses OIDC and
immutable image references; secrets remain in environment-specific secret
stores rather than source control.

## Delivery Rules

- Land package, image, infrastructure, and workflow behavior only within the
  approved scope; reserved paths do not imply that planned behavior exists.
- Validate product changes with path-focused checks plus contract tests at
  product boundaries.
- Keep deterministic engine code independent of wall-clock time, unseeded
  randomness, and direct network access.
- Gate deployment progressively: local tests, development infrastructure,
  bounded data soaks, paper and shadow qualification, then explicit live
  eligibility.


Research evidence crosses one shared boundary in `thesis.research.evidence`:
canonical parameter vectors retain JSON numeric types, and harness decimal
statistics are parsed for presentation without reconstructing trade ledgers.
Harness admission retains the immutable `accepted_input` document with the
single `job_id`; joining and publishing reject changed inputs. Thesis retains
the submitted-input digest before dispatch and checks every job-backed phase
assessment against the authoritative result and accepted input. The harness
owns closed trades, exact partial-close/flip fee allocation, gross/net fields,
and undefined-statistic reasons. Workspace imports retain entry/exit fees and
open fees rather than deriving another ledger.


Native replay records sparse causal mark updates with the observed fill-prefix
count. The canonical job ledger values every market/frame/fill sample using the
configured initial capital, including unallocated cash; fills are no longer the
only equity observations. Headline `equity.net_pnl` is final marked equity minus
initial capital and `equity.gross_pnl` adds all execution fees. Closed trade
`trades.net_pnl`/`gross_pnl` remain separate, with allocated entry/exit fees and
open-lot fees. API job receipts expose headline `net_pnl`/`gross_pnl` and explicit
`closed_trade_net_pnl`/`closed_trade_gross_pnl`; undefined statistic reasons are
retained. Imported workspace headline PnL uses the equity block without another
calculation, while closed-trade figures and profit factor retain their meaning.

The backtest results package owns financial statistics. The unused native metrics copy is removed; exact decimal primitives remain in the native kernel.

Research migration cleanup removes the retired trait-driven engine/result model, unused lineage package, dormant HTTP workflow adapter, typed tool argument wrappers, obsolete scanner, and inert workflow dependencies. Product composition supplies an explicit MCP-backed runner. Imported report statistics use one snake_case projection; UI headlines read the canonical harness quantities and undefined reasons.


Thesis runtime ownership ports live in `thesis_runtime/storage/ownership.py`; immutable
submission journaling lives in `persistence/submissions.py`. `thesis_runtime/execution/worker.py`
owns durable work execution policy. Cosmos ownership, work, submissions, execution
and chat adapters share the workspace-partitioned catalog. `thesis worker` runs
separately from stateless HTTP intake; `infra/modules/thesis.bicep` provisions its
warm execution capacity. See the runtime architecture's stateless ownership and
immutable submission sections for fencing, recovery, and deployment cutover.

### Managed wavelet research features

`strategy-sdk/src/wavelet.rs` owns the native `WaveletIndicator`, private trailing
completed-bar state and immutable `WaveletSnapshot` observations. Programs expose it
through MarketConsumer dependencies. Availability-bounded historical lookup and
retained snapshots prevent later observations from relabeling earlier decisions on
this supported path. Exact-decimal scale energies/coarse share and signed half-window
trend have explicit missing/warm-up behavior. The `wavelet` SDK reference is served
through strategy-jit validation and the existing MCP catalog. The Haar batch kernel
is internal to evidence publication, not the author-facing trading feature API.
Arbitrary authored Rust remains capable of computing other features or inventing
rows; the snapshot guarantee is not a general scientific sandbox.

## Fixed parallel workflow regions

The runtime supports structured fixed split/join graphs through `SplitPhase` and
existing deterministic phases. `thesis_runtime/workflows/graph/` contains only
pure topology validation and accepted-history control reconstruction; the workflow
host still owns acceptance and dispatch through its existing ports. No second
scheduler or graph store is introduced. Ordinary decisions move one position;
splits open fixed disjoint branches and joins restore one parent position.

Execution records project `active_phases` and `control_sequence`; transition
history retains explicit attempt and executor identities. Journal acceptance,
CAS and ownership remain authoritative. JSON extension protocol 2 supplies each
node's causal path, and paired joins receive fixed named accepted products.
Recovery preserves arrivals, reconciles accepted terminals and missing dispatch,
and may recompute only pure paired joins after interruption. Side-effecting work
keeps its retained-ID/no-auto-replay policy. Incoming messages continue through
Option 1's canonical writer and provider projection.

This capability does not activate parallelism in either current concrete workflow.
Hypothesis development 5.0.0 and research investigation 2.0.0 retain their existing
phase flows, prompts and tool policies; research still has one critic. Old runtime
records are rejected rather than inferred or upgraded. See
[the bounded implementation contract](thesis-fixed-workflow-graph-plan.md).
