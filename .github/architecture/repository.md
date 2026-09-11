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

The Rust migration is complete. The engine workspace lives under
`src/harness/rust/` and provides the authoritative engine core (clock, event
bus, replay, matching/fills, fees, portfolio, risk, results, sweeps, and the
backtest runtime) plus the `harness`, `api`, `mcp`, and `backtest-job`
processes. The Python engine under `src/harness` has been deleted; what remains
is the user-authored strategy/indicator/screener plugin surface, the
point-in-time market-data views the surviving products consume, and the
Python-side parity oracle. The Rust `harness-host` owns the strategy context
natively and duck-types it to the `harness.strategy.contracts.StrategyContext`
protocol, so strategies run unmodified without importing an engine. Zig was
retired in T253, and the Python engine paths were removed in the final
decommission wave; only the `trader` supervision runner remains deferred (see
below).

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
| `harness.configuration` | Strict API, backtest, and trader configuration; runtime-only secret references; canonical cross-machine hashes. |
| `harness.core` | Decimal-safe records, deterministic IDs, explicit clocks, and the synchronous event queue and bus. |
| `harness.data` | Point-in-time market-data views retained for the plugin surface and products: exact OHLCV bar derivation, the engine-clocked history and strategy-history capabilities, derived listing lifecycle events, the point-in-time option contract universe with DTE/strike/moneyness filters, the SecurityMaster listings view, and the composite universe view. Live and replay ingestion moved to the Rust `harness-data` and `harness-backtest` crates. |
| `harness.indicators` | Tick-native indicators, the deterministic router, and cross-mode snapshot/resume semantics. |
| `harness.screening` | Process-isolated execution sandbox for custom screener filters. |
| `harness.strategy` | Mode-independent strategy callbacks (`Strategy`, `CALLBACK_ORDER`), the protocol-only `StrategyContext` surface, the sandbox lint, and the callback-contract reference fixture. The engine-bound context and binding were deleted; the native Rust context replaces them. |
| `harness.testing` | Canonical artifact support, the Rust-core parity oracle, and the cross-mode tick-indicator parity oracle. |

`harness.oms`, `harness.fills`, `harness.portfolio`, `harness.risk`,
`harness.venues`, `harness.results`, `harness.sweeps`, `harness.http`,
`harness.mcp`, `harness.quant`, `harness.signal`, `harness.runtime`, and the
`harness.strategy.context` / `harness.strategy.binding` modules were deleted.
Their behavior is native Rust in `harness-native`, `harness-live`,
`harness-backtest`, `harness-num`, `harness-api`, and `harness-mcp`.

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

The deterministic backtest runner is the native Rust engine: it composes lake
replay, the simulated venue, fees, risk, portfolio accounting, and the fail-loud
look-ahead abort behind one engine clock, and emits a reproducibility manifest
covering configuration, code, seed, data, fill-model, fee, and risk
fingerprints. `backtest-job --config` runs it over the built-in deterministic
synthetic benchmark fixture, writing canonical run artifacts under
`.local/backtests/`.

The `harness`, `api`, `mcp`, and `backtest-job` processes are served by the Rust
binaries. The Python console scripts are thin shims (`harness.cli`) that exec
the matching binary; there is no Python engine behind them. `trader` is the one
deferred process: the Rust binary runs the deterministic live-core session demo
until the configured supervision runner is ported.

## Quant-Harness Rust Workspace

`src/harness/rust/` is a Cargo workspace providing the quant-harness engine and
its interfaces. The native engine core and the `harness`, `api`, `mcp`, and
`backtest-job` processes are authoritative; the Python engine under
`src/harness` was deleted and only the strategy plugin surface, the
market-data views, and the parity oracles remain. The Zig native core was
retired in T253, and `harness-native` is the native core the Python ctypes
loader consumes.

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
    |-- harness-live/          OMS, fills, venues, portfolio, risk port, and the `trader` binary
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
  reference fixture, sandbox lint, and the native strategy context. The context
  exposes clock, universe, books, orders, state, seeded random, emissions, and
  logging to a Python strategy without importing the Python engine. The walker
  parses strategy source with `rustpython-parser`; it no longer bridges to the
  Python AST checker, and `harness.strategy.lint` remains the shared-fixture
  parity oracle.
- `harness-config` reproduces the Python loader, strict models, exact-decimal
  normalization, datetime formatting, and domain-separated hashes; its parity
  suite asserts the exact hashes of every committed config file.
- `harness-data`, `harness-backtest`, and `harness-live` are engine ports with
  focused unit tests. `harness-backtest` backs the flipped `backtest-job`;
  `harness-live` provides the deterministic live core the deferred `trader`
  binary runs, but its configured supervision runner is not yet ported.

The workspace is gated by `cargo fmt --all --check`, `cargo clippy --workspace
--all-targets -- -D warnings`, and `cargo test --workspace`.

## Decommission Status

The Zig native core was retired in T253 (`build.zig`, `build.zig.zon`, and every
`.zig` source deleted), and the ctypes loader consumes the Rust `harness-native`
library. The engine now lives entirely in Rust under `src/harness/rust/`; the
Python engine was deleted in the final decommission wave, and the harness
console scripts are thin shims that exec the Rust binaries.

### Met criteria

1. `backtest-job` executes the Rust engine end to end. The `harness-api` binary
   drives `harness_host::backtest::run_backtest_job` over the configured
   `strategy_instances`.
2. The Rust `backtest-job` artifact is parity-proven against Python.
   `harness-backtest/tests/parity.rs` asserts byte-identical artifacts except
   `manifest.code_fingerprint`, which hashes each engine's own source by design.
3. The strategy plugin surface is decoupled from the engine. The Rust host owns
   the strategy context natively, and `harness.strategy` is protocol-only
   (`harness.strategy.contracts.StrategyContext`); the engine-bound
   `harness.strategy.context` and `harness.strategy.binding` were deleted.
4. The four harness console scripts (`harness`, `api`, `mcp`, `backtest-job`)
   resolve to Rust binaries; the Python engine modules that only those processes
   used were deleted with no dangling imports.

### Deferred: `trader`

The Rust `trader` binary composes the deterministic live core but does not yet
load configuration or supervise a paper/shadow session, so the configured
supervision runner is deferred. The Python supervision runtime was deleted with
the engine; restoring full trader function requires porting the supervision
runner (config loading, session resolution, feed supervision, boot reconciler,
kill-switch polling, and state snapshots) into `harness-live`.

### Retained Python engines

`pyproject.toml` keeps the dependencies the surviving Python product surfaces
require (`lake`, `lakeclient`, `thesis`, `ui`) plus the plugin surface
(`harness.configuration`, `harness.core`, `harness.data` views,
`harness.indicators`, `harness.screening`, `harness.strategy`,
`harness.testing`). The Python test suite covers those retained surfaces; the
Rust suites cover the engine.

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
  core library plus the `harness`, `api`, `backtest-job`, `mcp`, and `trader`
  binaries; the runtime stage carries the library under `/opt/rust` and the
  binaries under `/opt/rust/bin` (discoverable through
  `HARNESS_RUST_BINARY_DIR`).
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
