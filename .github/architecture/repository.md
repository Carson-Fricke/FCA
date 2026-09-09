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
implemented for the intraday-pattern research scripts. The pinned Zig 0.16.0
toolchain is a first-class build component: the `build.zig` module builds the
native core under `src/harness/native` (C ABI v2 with the Arrow C Data
Interface bridge, pinned scale-18 fixed point, and the ported event kernel,
OMS state machine, virtual book accounting, K-way replay merge, bar
derivation, resting-order matching venue, SimpleTouch, latency-size, and
trade-through fill models, the ported metrics modules, and the float64 batch
quant kernels — the Cox-Ross-Rubinstein option pricer with tree greeks plus
bracketed implied time-to-expiry and implied-underlying inversions), the
shared image compiles it in a dedicated Zig build stage, and CI gates the
native core with
`zig fmt --check` and `zig build test` alongside the Python gates. The
cross-language parity oracle drives the native core through a Zig-core runner
(`zig_backtest`) and asserts byte-identical determinism artifacts against the
pure-Python engine.

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

The `api` and `trader` command entrypoints remain thin process boundaries and
fail clearly until composed.

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
- `build.zig` and `build.zig.zon` define the pinned Zig 0.16.0 toolchain. They
  expose the `harness-native` module rooted at `src/harness/native`, install
  the native core library, and provide the `zig build test` smoke gate. CI
  enforces `zig fmt --check` and `zig build test` alongside the Python gates.
- `Dockerfile` builds one immutable image with distinct runtime commands. A
  pinned Zig build stage compiles the native core and tests it during image
  build, and the runtime stage carries the resulting library under
  `/opt/native`.
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
