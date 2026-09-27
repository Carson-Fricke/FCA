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
Rust `native` crate (C ABI v2 with the Arrow C Data Interface bridge,
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
`src/harness/` and provides the authoritative engine core (clock, event
bus, replay, matching/fills, fees, portfolio, risk, results, sweeps, and the
backtest runtime) plus the `harness`, `api`, `mcp`, and `backtest-job`
processes. The Python engine and the embedded-Python authoring surface have
been deleted: strategies, indicators, and studies are authored in Rust
against the `harness-strategy-sdk` crate and are JIT compiled and loaded
through `harness-strategy-jit` when a configuration runs (see
`rust-native-authoring-plan.md`). The interpreter is no longer a harness
build or test dependency. Zig was retired in T253, the Python engine paths
were removed in the final decommission wave, and the Python authoring
surface (host adapter, `py` extension, and the harness Python packages) was
removed in the native-authoring deletion; only the `trader` supervision
runner remains deferred (see below).

## Product Boundaries

| Boundary | Path | Responsibility |
| --- | --- | --- |
| `market-lake` | `src/lake` | The lake service (Rust workspace) and the retained Python library surface: provider ingestion, stable lake schemas, storage and catalog access, coverage planning, and the lake API/client surface. |
| `quant-harness` | `src/harness` | Deterministic simulation, OMS and portfolio behavior, results, paper and shadow runtimes, and capability-gated live execution. |
| `lakeclient` | `src/lakeclient` | Consumer-facing authenticated ensure, polling, file-query, and Parquet access without provider or Azure credentials. |
| `thesis` | `src/thesis` | Workspace-scoped research, coordination, persistence, authorization, and model-provider boundaries. |
| Authored programs | `config/harness/*.rs` and inline `parameters.source` | User-authored Rust strategies, indicators, and study programs compiled against the authoring SDK. |

Dependency direction is from the harness and strategies toward published
contracts, never from the lake into harness runtime code. The harness may use
lake schemas, catalog records, APIs, and clients, but the lake must remain
operable without the harness. Strategies depend on the harness contract rather
than engine internals.

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
(`config`, `num`, `marketdata`, `backtest`, `live`, `api`, `mcp`,
`strategy-sdk`, `strategy-jit`). Thesis code that still imports the removed
packages is expected to break until it migrates to the new surfaces.

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
fingerprints. `backtest-job --config` runs it over a lake data directory with
JIT-authored programs, writing canonical run artifacts under `.local/`.

The `harness`, `api`, `mcp`, and `backtest-job` processes are served by the Rust
binaries directly; the Python console-script shims were deleted with the
harness Python packages. `trader` is the one
deferred process: the Rust binary runs the deterministic live-core session demo
until the configured supervision runner is ported.

## Quant-Harness Rust Workspace

`src/harness/` is a Cargo workspace providing the quant-harness engine and
its interfaces, and it contains no Python: the engine, the process binaries,
and the authoring surface are all native Rust. The native engine core and the
`harness`, `api`, `mcp`, and `backtest-job` processes are authoritative. The
embedded-Python host, the `py` extension crate, and the harness Python
packages were deleted in the native-authoring wave.

```text
src/harness/
|-- Cargo.toml                 Workspace manifest (resolver 2)
|-- rust-toolchain.toml        Pinned stable toolchain
|-- native/                    C ABI native core (`cdylib` + `rlib`)
|-- num/                       Numeric kernels
|-- marketdata/                Market-data port
|-- backtest/                  Backtest, sweeps, and results port
|-- live/                      OMS, fills, venues, portfolio, risk port, and the `trader` binary
|-- api/                       CLI facade, axum HTTP boundary, api/mcp/backtest-job binaries
|-- mcp/                       JSON-RPC 2.0 / MCP server and tool catalog
|-- strategy-sdk/              Authoring SDK: traits, versioned C ABI, entry-point macros
|-- strategy-jit/              JIT seam: managed workspace, cargo build, libloading, engine adapters
`-- config/                    YAML-subset loader, models, canonical hashing
```

- `api` owns the `harness`, `api`, `backtest-job`, and `mcp` process
  binaries; its axum routes, bearer authentication, and provenance fields
  mirror the retired `harness.http` module. The `backtest-job` binary drives
  the native engine through the `harness-strategy-jit` seam.
- `mcp` owns the pure JSON-RPC dispatch, the capability-filtered tool
  catalog, bearer credential resolution, and newline-delimited stdio framing.
- `strategy-sdk` exposes the authoring traits (strategy, indicator, study
  program) over the engine's event types with the capability split enforced
  by construction: a study program has no order surface. Its versioned C ABI
  is the only contract that crosses a dylib boundary, and the loader rejects
  artifacts whose ABI version differs.
- `strategy-jit` writes authored sources into a content-addressed managed
  cargo workspace with one shared `target/`, compiles them with the pinned
  toolchain, caches by source digest, loads the cdylib through `libloading`,
  and adapts it to the engine's `StrategyRuntime` seam. Compilation output,
  the toolchain digest, and the source digest feed the bundle-hash provenance
  flow; a configuration's `bundle_hash` must equal the source digest.
- `config` reproduces the retired Python loader's strict models,
  exact-decimal normalization, datetime formatting, and domain-separated
  hashes; its parity suite asserts the exact hashes of every committed config
  file.
- `marketdata`, `backtest`, and `live` are engine ports with
  focused unit tests. `backtest` backs the flipped `backtest-job`;
  `live` provides the deterministic live core the deferred `trader`
  binary runs, but its configured supervision runner is not yet ported.

The workspace is gated by `cargo fmt --all --check`, `cargo clippy --workspace
--all-targets -- -D warnings`, and `cargo test --workspace`; no Python
interpreter is required.

## Market-Lake Rust Workspace

`src/lake/` is a Cargo workspace at the product root whose `Cargo.toml` and the
Python package `lake/` coexist, exactly as `src/harness/` does. The lake
service is authoritative: the `lake` binary serves the published catalog,
ensure, jobs, query, and raw routes, and the `lake` console script is a thin
Python shim (`lake.cli`) that execs it, with binary discovery through
`LAKE_RUST_BINARY_DIR` and `src/lake/target/{release,debug}`.

```text
src/lake/
|-- Cargo.toml                 Workspace manifest (resolver 2)
|-- rust-toolchain.toml        Pinned stable toolchain
|-- contracts/                 Asset classes, data types, sessions, partitions, Arrow schemas
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
a configuration, JIT compiled on submit, and driven by the native engine
through the versioned C ABI. The implemented contract is:

- Authoring. A study program implements `StudyProgram` (`on_quote`,
  `on_trade`, `on_stop(publisher)`) and exports itself with
  `export_study_program!`; a strategy program implements `StrategyProgram`
  (`on_start`, `on_quote`/`on_trade` with an order sink, `on_fill`,
  `on_stop`) and exports itself with `export_strategy_program!`. One
  authored crate exports exactly one program. The SDK exposes only the
  capability surface the author is allowed: a study has no order surface by
  construction (the vtable carries no order path), mirroring the retired
  `ResearchModelProgram` contract.
- Submission. `strategy_instances[].implementation` must be `"jit"`. The
  authored source travels as `parameters.source` (inline, for JSON
  configurations) or as `parameters.source_path` (a file relative to the
  configuration document, resolved at load time). `bundle_hash` must equal
  the SHA-256 of the authored source; the JIT seam rejects a mismatch.
- Loading. On submit the seam writes the authored crate into the managed
  workspace, compiles it with the pinned toolchain (cached by source digest,
  SDK version, and toolchain digest), loads the cdylib through `libloading`,
  checks the exported ABI version against the host's, constructs the program
  state from the configured `parameters`, and drives it through the engine's
  `StrategyRuntime` trait. A panic or failure inside authored code is
  contained and reported as a strategy-execution error.
- Metrics. `publish_metric(name, value)` accepts a finite numeric value
  under a non-reserved stable name; every rejection is recorded host-side and
  fails the run even when the author ignores the returned error. Repeated
  publication replaces the prior value; final values are written to
  `metrics.json.custom`, the canonical run artifact, the recorder's
  `emissions.parquet`, and the local/HTTP run catalog. Metric names are run
  outputs, not indicator registrations.
- Guidance. The plan and status live in
  `.github/architecture/rust-native-authoring-plan.md`; the reference port
  and re-pinned parity expectations are the next phases.

## Decommission Status

The Zig native core was retired in T253 (`build.zig`, `build.zig.zon`, and every
`.zig` source deleted). The engine lives entirely in Rust under `src/harness/`;
the Python engine was deleted in the final decommission wave, and the
embedded-Python authoring surface (host adapter, `py` extension, harness
Python packages, and the `strategies/` tree) was deleted in the
native-authoring wave. The harness process binaries are served directly; no
harness build or test needs a Python interpreter.

### Met criteria

1. `backtest-job` executes the Rust engine end to end. The `api` binary
   drives `harness_strategy_jit::run_backtest_configuration` over the
   configured `strategy_instances`.
2. Authored Rust programs run through the engine from JIT-loaded dylibs,
   proven by the `strategy-jit` suite (study and strategy runs over a real
   Parquet lake, capability gate, panic containment, metric rejection).
3. The authoring surface is decoupled from the engine. The SDK owns the
   authoring traits and the versioned C ABI; the engine consumes only the
   `StrategyRuntime` seam.
4. The harness process binaries (`harness`, `api`, `mcp`, `backtest-job`)
   are the only serving surfaces; the Python console-script shims were
   deleted with no dangling harness imports.

### Deferred: `trader`

The Rust `trader` binary composes the deterministic live core but does not yet
load configuration or supervise a paper/shadow session, so the configured
supervision runner is deferred. The Python supervision runtime was deleted with
the engine; restoring full trader function requires porting the supervision
runner (config loading, session resolution, feed supervision, boot reconciler,
kill-switch polling, and state snapshots) into `live`.

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
`thesis.identity` owns durable accounts, one-time invite links, and
server-side sessions for the invite-only public release (ADR-001);
`thesis.research` owns content-addressed research models, explicit
`IndicatorRef` scope, and workspace indicator publication. The immutable
global indicator registry remains in `harness.indicators`; workspace indicator
source is published under `indicators/<name>/<content-sha>/` and never mutates
or shadows that registry. Research models are published under
`models/<model-id>/<content-sha>/`, compose explicitly scoped indicator refs,
and publish each run's evidence under `models/runs/`. `thesis.strategies` owns
content-addressed strategy bundles; `thesis.artifacts`
owns reproducibility-critical result imports; `thesis.backtests` imports
completed harness runs into workspace backtest records (equity, trades, stats)
linked to the originating chat and workflow execution; and `thesis.providers`
keeps model credentials behind backend-only protocols.
Workspace identity remains `.thesis/workspace.yaml`; publication state is
written separately to `.thesis/publication.json`.

## Shared Foundation

The products share repository-level mechanics where divergence would create
operational drift:

- `pyproject.toml` defines the Python project and common quality tooling.
- `src/harness/` is a Cargo workspace with a pinned stable toolchain. It
  builds `native` (the native core the Python loader consumes) and the
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
