# Repository Architecture

<!-- markdownlint-disable MD013 -->

## Status

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
backtest results store with its run catalog, standard metrics and attribution,
the point-in-time option contract universe, the capability-scoped strategy
boundary (read-only clock, opaque StrategyHistory data capability, one-way
venue commands, provenance-restricted scheduling), live-feed gap healing, the
SecurityMaster listings view, the PaperShell shared paper simulation runtime,
and the local `backtest-job` CLI are implemented; offline reports, the harness
query API, shadow/live runtimes, and CI/deployment orchestration remain
planned. The `harness.signal` pure-NumPy wavelet and analytic-signal kernels
(MODWT with circular/reflection boundaries, additive MRA, wavelet variance,
Hilbert envelope/phase features, and an exploratory Morlet CWT scalogram) are
implemented for the intraday-pattern research scripts. The native core is the
Rust `harness-native` crate (C ABI v2 with the Arrow C Data Interface bridge,
pinned scale-18 fixed point, and the ported event kernel, OMS state machine,
virtual book accounting, K-way replay merge, bar derivation, resting-order
matching venue, SimpleTouch, latency-size, and trade-through fill models, the
ported metrics modules, and the float64 batch quant kernels — the
Cox-Ross-Rubinstein option pricer with tree greeks plus bracketed implied
time-to-expiry and implied-underlying inversions). The pinned Zig toolchain and
every `.zig` source were retired in T253; the shared image compiles the native
library in a Rust build stage, and CI gates it with `cargo fmt --all --check`,
`cargo clippy`, and `cargo test`. The cross-language parity oracle drives the
native core through `harness.runtime.zig_backtest` (retained module name) and
`harness.testing.rust_oracle`, asserting byte-identical determinism artifacts
against the pure-Python engine.

The Rust migration is at its final structural stage. The engine workspace now
lives under `src/harness/rust/` and provides the authoritative engine core
(clock, event bus, replay, matching/fills, fees, portfolio, risk, results, and
the backtest runtime) plus the `api`, `mcp`, and `backtest-job` processes. The
Python engine under `src/harness` is retained only where active Python code
still imports it: the user-authored strategy/indicator plugin surface, the
Python `trader` supervision runtime, and the reference fixtures the parity
oracle compares against. The strategy contract, binding, reference fixture, and
sandbox lint are ported natively, and the sandbox walker no longer bridges to
the Python AST checker. Zig is already retired; removal of the remaining Python
engine paths is gated by the decommission criteria below.

## Product Boundaries

| Boundary | Path | Responsibility |
| --- | --- | --- |
| `market-lake` | `src/lake` | Provider ingestion, stable lake schemas, storage and catalog access, coverage planning, and the lake API/client surface. |
| `quant-harness` | `src/harness` | Deterministic simulation, OMS and portfolio behavior, results, paper and shadow runtimes, and capability-gated live execution. |
| `lakeclient` | `src/lakeclient` | Consumer-facing authenticated ensure, polling, file-query, and Parquet access without provider or Azure credentials. |
| `thesis` | `src/thesis` | Workspace-scoped research, coordination, persistence, authorization, and model-provider boundaries. |
| Strategies | `strategies` | User-authored implementations of the public harness strategy contract. |

Dependency direction is from the harness and strategies toward published
contracts, never from the lake into harness runtime code. The harness may use
lake schemas, catalog records, APIs, and clients, but the lake must remain
operable without the harness. Strategies depend on the harness contract rather
than engine internals.

## Market-Lake Packages

`src/lake` is organized by capability rather than by deployment layer. Public
package facades expose deliberate contracts; provider SDKs, FastAPI, Azure
adapters, and orchestration details stay behind those boundaries.

| Package | Responsibility |
| --- | --- |
| `lake.catalog` | Coverage, job, contract, front-set, listing, and run records; repository ports; memory and Azure Table adapters. |
| `lake.cli` | Console dispatch and API, ingest, or local process commands. |
| `lake.configuration` | Strict YAML loading, typed settings, environment overrides, and normalized universes. |
| `lake.contracts` | Stable partition paths, PyArrow schemas, and exchange or UTC session contracts. |
| `lake.coverage` | Calendar-aware listings-aware coverage classification (including the terminal delisted state), gap planning, and ensure-oriented application behavior. |
| `lake.hosted` | Hosted Azure composition that wires the published Uvicorn target to Table catalog/jobs, SAS issuance, and bounded Alpaca ensure. |
| `lake.http` | FastAPI composition, authentication, request models, and thin catalog, ensure, and query controllers. |
| `lake.ingestion` | Shared ingest ports plus equity, option, and crypto planning, transactions, and worker construction. |
| `lake.local` | Workstation-only API, Alpaca ingest, filesystem, and catalog composition. |
| `lake.providers.alpaca` | Async Alpaca transport, pagination/retry behavior, and normalized provider records. |
| `lake.query` | File-query orchestration and bounded DuckDB execution over ensured objects. |
| `lake.queue` | Durable delivery policy, Azure queue adapter, dead-letter envelopes, and explicit replay. |
| `lake.storage` | Storage ports, Parquet serialization, Azure blob/SAS adapters, the readable blob surface (read, list, prefix delete), capacity guards, and leases. |

```text
src/lake/
|-- app.py                         Published Uvicorn facade (`lake.app:app`)
|-- catalog/                       Records, repository ports, and adapters
|-- cli/                           `lake` command implementation
|-- configuration/                 Settings and universe loading
|-- contracts/                     Paths, schemas, and sessions
|-- coverage/                      Coverage domain and application services
|-- hosted/                        Hosted Azure composition for `lake.app:app`
|-- http/
|   `-- routes/                    Thin authenticated HTTP controllers
|-- ingestion/
|   |-- equities/                  Equity transaction and backfill behavior
|   |-- options/                   Front-set selection and option ingestion
|   `-- crypto/                    UTC-day planning and crypto ingestion
|-- local/                         Workstation-only composition and state
|-- providers/
|   `-- alpaca/                    Provider records and async HTTP client
|-- query/                         File and bounded SQL query services
|-- queue/                         Worker, Azure adapter, and dead letters
`-- storage/                       Ports, Parquet, Azure, capacity, and leases
```

| Thesis workflows | `src/thesis/workflows` | Orchestrated workspace workflows: strategy-development vertical slice (chat request → generation → validation → backtest → artifact import → evidence journaling). |

The thesis backend has a real process boundary: `thesis` console entrypoint and module entrypoint (`src/thesis/cli.py`, `src/thesis/__main__.py`) with a dedicated runtime image (`Dockerfile.thesis`, first built in CI publication).

Dependency direction flows inward. HTTP routes and CLI commands invoke
application services; application services depend on catalog, queue, storage,
and provider ports; Azure, Alpaca, FastAPI, and DuckDB implementations remain
at adapter boundaries. Domain contracts do not import HTTP or CLI code.

Tests mirror the capability layout under `tests/lake`:

```text
tests/lake/
|-- catalog/
|-- cli/
|-- configuration/
|-- contracts/
|-- coverage/
|-- hosted/
|-- http/routes/
|-- ingestion/{equities,options,crypto}/
|-- providers/alpaca/
|-- query/
|-- queue/
`-- storage/
```

## Quant-Harness Packages

`src/harness` mirrors the product-first structure under `tests/harness`.

| Package | Responsibility |
| --- | --- |
| `harness.configuration` | Strict API, backtest, and trader configuration; runtime-only secret references; canonical cross-machine hashes. |
| `harness.core` | Decimal-safe records, deterministic IDs, explicit clocks, and the synchronous event queue and bus. |
| `harness.oms` | Execution-report-driven single-leg lifecycle, exact fill ledgers, replacements, and session directives. |
| `harness.data` | Live/replay market-data boundaries, synchronous normalized event inboxes, exact OHLCV bar derivation, bounded lake replay (equities and options over the point-in-time option universe), the engine-side HistoricalView with engine-clock-capped queries (future reads raise `LookaheadError`) plus the opaque engine-clocked StrategyHistory strategy capability whose now-anchored reads take no timestamp parameters so future data is unrepresentable, live-feed gap healing through market-lake backfill, the point-in-time option contract universe over the market-lake contracts catalog (front-set and static-OSI modes, as-of DTE/strike/moneyness filters, expiry-indexed snapshots clipped to the engine clock), the SecurityMaster point-in-time listings view with derived delisting and rename lifecycle events, the clock-guarded composite UniverseView over listing and contract sources, the market-lake access adapter for ensure/query-driven backtest data, Alpaca SIP, crypto, and OPRA WebSocket feeds with UTC-day crypto semantics and live contract rotation, and the live event recorder writing lake-schema Parquet under `live/{deployment}`. |
| `harness.fills` | Configuration-selected exact fee and commission models plus deterministic SimpleTouch, latency-size, trade-through, and MidTouch option fill decisions. |
| `harness.http` | Backtest query API and job launcher: authenticated run listing, artifact fetch, and launch routes composing the results store and the harness api process. |
| `harness.portfolio` | Strategy virtual books, shared-account aggregate accounting, and deterministic option expiry, exercise, and assignment settlement. |
| `harness.quant` | NumPy facade over the native float64 quant kernels: batch Cox-Ross-Rubinstein option pricing with tree greeks and bracketed implied time-to-expiry and implied-underlying inversions. |
| `harness.results` | Streaming Parquet run recorder with referentially linked actions, orders, fills, risk decisions, equity, positions, emissions, and full-event artifacts; standard metrics and attribution with documented conventions; offline static HTML reports; and the backtest results store writing run artifacts under `backtests/{run_id}` with a filterable run catalog and prefix-complete cleanup, wired to the lake run-catalog and readable-blob ports for production Azure adapters. |
| `harness.runtime` | Deterministic backtest runner: data preflight, lake replay, simulated venue, fees, risk, portfolio accounting, reproducibility manifest, and abort/halt/teardown policies; plus the PaperShell shared simulation runtime composing the same kernel, venue, fills, fees, risk, and portfolio accounting against replay or live inbox feeds, the runtime state store capturing hash-verifiable snapshots with restore-equivalent continuation, the boot/periodic reconciler with broker-vs-book diff artifacts and explicit policies, and the recovery boot policies (resume-reconcile, flatten-halt, halt-only) with chaos-proven fault handling. |
| `harness.strategy` | Mode-independent strategy callbacks, constrained context with capability-scoped surfaces (read-only clock, opaque engine-clocked data history, one-way venue commands, provenance-restricted scheduling, and engine-delivered corporate-action hooks), deterministic subscriptions, class-plus-parameter binding, sandbox lint, and a callback-contract reference strategy. |
| `harness.risk` | Ordered exact-decimal pre-trade risk rules, reductions, halts, complete decision auditing, options collateral rules (naked-short rejection and collateral limits with audited decisions), and the explicit audited global kill switch. |
| `harness.signal` | Pure-NumPy time-series signal kernels for market-microstructure research: maximal-overlap discrete wavelet transform (MODWT) with circular/reflection boundaries, additive multiresolution analysis, exact inversion, and unbiased wavelet-variance estimation; Hilbert analytic-signal envelope and phase features for band-limited details; and an exploratory Morlet continuous-wavelet-transform scalogram. |
| `harness.testing` | Deterministic synthetic feeds, determinism artifact canonicalization, reproducibility manifest comparison, cross-language parity scenarios, and paper-to-backtest parity scenarios and reports. |
| `harness.venues` | Fire-and-forget execution boundary, deterministic scripted venue, simulated top-of-book matching venue, the options multi-leg matching engine with atomic all-leg fillability, and the Alpaca live broker venues for single-leg equities (async command receipts, execution-report conversion), fractional continuous crypto with maker-taker fee reconciliation, and options multi-leg (net debit/credit spreads, leg-report mapping) behind the consolidated capability matrix. |

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
raise `LookaheadError` as defense-in-depth). Strategy sandbox lint checks
wall-clock, sleep, network, subprocess, unseeded-random, asyncio, and
concurrency patterns as a CI gate over the strategy examples, not as a
run-path check.

The deterministic backtest runner composes lake replay, the simulated venue,
fees, risk, portfolio accounting, and the fail-loud look-ahead abort behind
one `SimClock`, and emits a reproducibility manifest covering configuration, code,
seed, data, fill-model, fee, and risk fingerprints. `backtest-job --config`
runs it locally over the built-in deterministic synthetic benchmark fixture or
a user-supplied local Parquet lake directory, writing canonical run artifacts
under `.local/backtests/`.

The `api` and `mcp` processes are served by the Rust binaries, and
`backtest-job` runs the native Rust engine; the Python console scripts dispatch
to them behind `HARNESS_USE_RUST_ENTRYPOINTS`. `trader` remains a thin Python
process boundary until its Rust supervision runner is ported.

## Quant-Harness Rust Workspace

`src/harness/rust/` is a Cargo workspace providing the quant-harness engine and
its interfaces. The native engine core and the `api`, `mcp`, and `backtest-job`
processes are authoritative; the Python engine under `src/harness` remains only
for the Python plugin surface, the `trader` runtime, and the parity reference.
The Rust interface processes are flipped behind
`HARNESS_USE_RUST_ENTRYPOINTS` (see `rust-interface-migration.md`). The Zig
native core was retired in T253, and `harness-native` is the native core the
Python ctypes loader consumes.

```text
src/harness/rust/
|-- Cargo.toml                 Workspace manifest (resolver 2)
|-- rust-toolchain.toml        Pinned stable toolchain
`-- crates/
    |-- harness-native/        C ABI native core (`cdylib` + `rlib`)
    |-- harness-py/            Python extension bindings (`_rust` cdylib)
    |-- harness-num/           Numeric kernels
    |-- harness-data/          Market-data port
    |-- harness-backtest/      Backtest, sweeps, and results port
    |-- harness-live/          OMS, fills, venues, portfolio, and risk port
    |-- harness-api/           CLI facade, axum HTTP boundary, api/mcp/backtest-job binaries
    |-- harness-mcp/           JSON-RPC 2.0 / MCP server and tool catalog
    |-- harness-host/          PyO3 strategy, indicator, and screener host
    `-- harness-config/        YAML-subset loader, models, canonical hashing
```

- `harness-api` owns the `harness`, `api`, `backtest-job`, and `mcp` process
  binaries; its axum routes, bearer authentication, and provenance fields
  mirror `harness.http`. The `backtest-job` binary drives the native engine
  through the `harness-host` PyO3 strategy adapter and is parity-proven against
  the Python reference.
- `harness-mcp` owns the pure JSON-RPC dispatch, the capability-filtered tool
  catalog, bearer credential resolution, and newline-delimited stdio framing.
- `harness-host` embeds the interpreter for user strategy, indicator, and
  screener execution and owns the native Rust strategy contract, binding,
  reference fixture, and sandbox lint. The walker parses strategy source with
  `rustpython-parser`; it no longer bridges to the Python AST checker, and
  `harness.strategy.lint` remains the shared-fixture parity oracle.
- `harness-config` reproduces the Python loader, strict models, exact-decimal
  normalization, datetime formatting, and domain-separated hashes; its parity
  suite asserts the exact hashes of every committed config file.
- `harness-data`, `harness-backtest`, and `harness-live` are engine ports with
  focused unit tests. `harness-backtest` backs the flipped `backtest-job`;
  `harness-live` is not yet wired into the interface binaries, so the Python
  `trader` runtime remains authoritative.

The workspace is gated by `cargo fmt --all --check`, `cargo clippy --workspace
--all-targets -- -D warnings`, and `cargo test --workspace`.

## Decommission Status

The Zig native core is already retired: T253 deleted `build.zig`,
`build.zig.zon`, and every `.zig` source, and the ctypes loader now consumes the
Rust `harness-native` library. The engine core now lives in Rust under
`src/harness/rust/`, the `api`, `mcp`, and `backtest-job` entrypoints are
flipped, and the native `backtest-job` is parity-proven. The Python engine is
retained only where active Python code or the parity oracle still imports it,
and only modules with zero importers are deleted.

### Gate

The remaining Python engine paths close when every criterion holds and
repository-wide verification is green on the Rust stack.

1. ~~`backtest-job` executes the Rust engine end to end.~~ Met: the
   `harness-api` binary drives `harness_host::backtest::run_backtest_job` over
   the configured `strategy_instances`.
2. ~~The Rust `backtest-job` artifact is parity-proven against Python.~~ Met:
   `harness-backtest/tests/parity.rs` asserts byte-identical artifacts except
   `manifest.code_fingerprint`, which hashes each engine's own source by design.
3. `trader` has a Rust supervision runner. The Rust `trader` binary is a
   deterministic core demo that does not load configuration or supervise a
   session, so the Python runtime remains authoritative.
4. The retained Python strategy plugin surface is decoupled from the engine.
   `harness.strategy.context` still imports `harness.data`, `harness.portfolio`,
   `harness.venues`, and `harness.core`, so the protocol-only-imports refactor
   is a prerequisite for deleting those engine modules. The target state is
   recorded as an expected failure in
   `tests/harness/strategy/test_import_boundary.py`.

### Staged, not deleted

Until the gate closes:

- Docker and CI keep the Python test and packaging gates. The Zig build stage
  and `zig build test` gate are gone (T253); the Docker `rust` stage builds and
  tests the workspace under `src/harness/rust/`, and the runtime image ships the
  Rust entrypoint binaries under `/opt/rust/bin` for the gated flip.
- `pyproject.toml` keeps its engine dependencies because the retained Python
  plugin surface, the `trader` runtime, and the parity reference still import
  them. Pruning them to the strategy-host surface is gated on closing the
  remaining engine paths.
- The Python suites for the retained surfaces remain as parity oracles
  (`tests/harness/http`, `tests/harness/mcp`, `tests/strategy_lint`,
  configuration, and the Rust and native determinism oracles) alongside the
  Rust parity suites.
- The entrypoint flip covers `api`, `mcp`, and `backtest-job`. `trader` is
  wired to the flip switch but deferred, and `backtest-job --data-dir` stays on
  Python until the native engine wires a lake reader.
  `rust-interface-migration.md` tracks the flip itself.

## Thesis Packages

Thesis remains a peer product. `thesis.workspaces` owns isolated workspace
identity and repository scaffolds; `thesis.cleo` serializes workspace-local
planning mutations; `thesis.persistence` journals accepted commands and fences
Git publication; `thesis.auth` binds operator sessions to workspaces;
`thesis.chat`, `thesis.execution`, and `thesis.http` define the UI-facing
workflow boundary; `thesis.agent` owns policy-driven model context and
provenance; `thesis.integrations` owns lake and harness adapters;
`thesis.strategies` owns content-addressed strategy bundles; `thesis.artifacts`
owns reproducibility-critical result imports; and `thesis.providers` keeps model
credentials behind backend-only protocols.
Workspace identity remains `.thesis/workspace.yaml`; publication state is
written separately to `.thesis/publication.json`.

## Shared Foundation

The products share repository-level mechanics where divergence would create
operational drift:

- `pyproject.toml` defines the Python project and common quality tooling.
- `src/harness/rust/` is a Cargo workspace with a pinned stable toolchain. It
  builds `harness-native` (the native core the Python loader consumes) and the
  harness interface binaries. CI enforces `cargo fmt --all --check`,
  `cargo clippy --workspace --all-targets -- -D warnings`, and
  `cargo test --workspace` alongside the Python gates. The pinned Zig toolchain
  from the earlier native-core effort was retired in T253.
- `Dockerfile` builds one immutable image with distinct runtime commands. A
  Rust build stage compiles and tests the workspace and produces the native
  core library plus the `harness`, `api`, `backtest-job`, and `mcp` binaries;
  the runtime stage carries the library under `/opt/rust` and the binaries
  under `/opt/rust/bin` (discoverable through `HARNESS_RUST_BINARY_DIR`).
- `config` contains versioned, non-secret YAML grouped by product or concern.
- `infra` contains reusable Bicep modules, environment parameters, identity,
  storage, observability, and Container Apps definitions.
- `.github/workflows` provides path-filtered checks, image publication, and
  OIDC-based deployment.
- `tests/lake` and `tests/harness` mirror their product capability boundaries.
  `tests` also holds explicit cross-product contract tests where integration is
  intentional.

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

- Land package, image, infrastructure, and workflow behavior only in the CLEO
  task that owns it; reserved paths do not imply that planned behavior exists.
- Validate product changes with path-focused checks plus contract tests at
  product boundaries.
- Keep deterministic engine code independent of wall-clock time, unseeded
  randomness, and direct network access.
- Gate deployment progressively: local tests, development infrastructure,
  bounded data soaks, paper and shadow qualification, then explicit live
  eligibility.

The authoritative implementation sequence and cross-product dependencies are
in `.cleo-export.json`, rooted at `T001` for `market-lake` and `T068` for
`quant-harness`.
