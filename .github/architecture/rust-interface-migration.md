# Rust Interface Migration

<!-- markdownlint-disable MD013 -->

## Status

The quant-harness engine core and the process interfaces are native Rust in the
workspace under `src/harness/`. The engine core — clock, event bus, replay,
matching/fills, fees, portfolio, risk, results, sweeps, and the backtest runtime
— is Rust, and the Python engine that used to mirror it has been deleted.

User-authored strategies, indicators, and screener programs remain Python. The
Rust engine drives them through the PyO3 plugin host in `host`; the
host owns the strategy context natively (clock, universe, books, orders, state,
seeded random, emissions, logging, and finite named metric publication) and duck-types it to the documented
`harness.strategy.contracts.StrategyContext` protocol, so strategies run
unmodified without importing an engine. Strategies and indicators keep
authoring against `src/harness/strategy/**`, `src/harness/indicators/**`, and
the example strategies under `strategies/`.

The native `backtest-job` runs the Rust engine end to end over the configured
`strategy_instances` and is parity-proven byte-for-byte against the Python
reference under the agreed definition (see below).

## Entrypoints

Every harness console script is a thin shim that execs the matching Rust
binary. The shims live in `src/harness/cli/` and resolve binaries through
`harness.cli.rust_entrypoints`.

| Console script | Rust binary | Rust crate | Status |
| --- | --- | --- | --- |
| `harness` | `harness` | `api` | flipped; facade dispatches `api`, `mcp`, and `backtest-job` |
| `api` | `api` | `api` | flipped (axum HTTP boundary, bearer auth, provenance) |
| `backtest-job` | `backtest-job` | `api` | flipped (native engine, parity-proven) |
| `trader` | `trader` | `live` | flipped (native supervision runner: config, sessions, feed supervision, reconciler, kill-switch, snapshots) |
| `harness mcp` | `harness` | `api` | flipped (JSON-RPC dispatch, tool catalog, stdio transport) |

Configuration is ported behavior-identically in `config` (loader,
strict models, canonical hashing) and the strategy host surface in
`host` (PyO3 strategy/indicator/screener hosts).

Binary discovery, in order:

1. `HARNESS_RUST_BINARY_DIR`.
2. `src/harness/target/release`.
3. `src/harness/target/debug`.

A shim exits with code 2 and an actionable message when the binary has not been
built; it never falls back to a deleted Python implementation.

### Backtest lake data (--data-dir)

`backtest-job --data-dir <dir>` reads a local Hive-partitioned Parquet lake through `marketdata::LocalLakeReader` and drives the native engine over the real data instead of the synthetic fixture. Symbols shaped like a crypto pair (`BTC-USD`) additionally read their `BASE/QUOTE` crypto partition and are re-keyed to the configured dashed symbol. The canonical parity test is unchanged.

Before a local run, the harness API asks the market-lake ensure contract (`LAKE_API_URL`, `LAKE_API__BEARER_TOKEN`) to fetch every configured symbol/data-type for the run window, then polls coverage to a terminal status. The lake pages each fetch until the provider cursor is exhausted, so a closed session is complete rather than truncated, and the harness splits long windows into lake-sized ranges and fails fast when a closed session is left `partial` or errored. The lake owns provider credentials and the fetch; the harness never reads them. A symbol set the lake has not ingested before is thus fetched on demand rather than failing preflight with a missing-symbol error.

## Parity definition

Data-bearing artifacts are byte-identical across the two stacks except
`manifest.code_fingerprint`, which hashes each engine's own source by design.
The golden comparison in
`src/harness/backtest/tests/parity.rs` masks that one field
in both payloads and newline-normalizes them before comparing.

## Python surface retained

The Python engine was deleted. What remains under `src/harness` is the plugin
surface and the market-data views the surviving products consume:

* `harness.configuration` and `harness.core` — strict configuration and the
  decimal-safe records, symbols, clocks, and event bus.
* `harness.strategy` — the protocol-only plugin contract (`StrategyContext`),
  the callback contract, the sandbox lint, and the reference fixture. The
  engine-bound context and binding were deleted; the native Rust context
  replaces them.
* `harness.indicators` — the immutable global registry and tick-native built-in
  kernels. Private mutable registries remain available for tests and injected
  extensions, but workspace publication never mutates the global registry.
* `harness.research` — the no-order adapter and restricted namespace for
  workspace statistical models and workspace indicator functions. Source
  receives plain event/series mappings and returns finite numeric outputs.
* `harness.screening` — user screener kernels and their process-isolated sandbox.
* `harness.data` — the point-in-time views and option filters retained for the
  plugin surface and the thesis product: bars, history, listing events, the
  option contract universe (`DteFilter`, `StrikeFilter`, `MoneynessFilter`),
  the SecurityMaster view, and the composite universe view.
* `harness.testing` — canonical artifact support, the Rust-core parity oracle,
  and the cross-mode indicator parity oracle.
* `harness.cli` — the console-script shims into the Rust binaries.

Deleted engine modules: `harness.runtime`, `harness.oms`, `harness.fills`,
`harness.portfolio`, `harness.risk`, `harness.venues`, `harness.results`,
`harness.sweeps`, `harness.http`, `harness.mcp`, `harness.quant`,
`harness.signal`, `harness.strategy.context`, `harness.strategy.binding`, the
live/replay ingestion modules under `harness.data`, and the Python tests that
covered only those modules.
