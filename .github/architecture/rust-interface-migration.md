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
seeded random, emissions, and logging) and duck-types it to the documented
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
| `trader` | `trader` | `live` | deferred; see below |
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

### Deferred: `trader`

The Rust `trader` binary composes the deterministic live core (risk pipeline,
OMS, simulated venue, and portfolio accounting) but does not yet load
configuration or supervise a paper/shadow session. The configured supervision
runner — config loading, session resolution, feed supervision, the boot
reconciler, kill-switch polling, and state snapshots — is not ported, so the
process is deferred and the Rust binary runs the deterministic live-core
session demo. The Python supervision runtime (`harness.runtime`,
`harness.oms`, `harness.fills`, `harness.portfolio`, `harness.risk`,
`harness.venues`, and the live data feeds) was deleted with the rest of the
Python engine; restoring full trader function requires porting the supervision
runner first.

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
* `harness.indicators` and `harness.screening` — user indicator and screener
  kernels and their process-isolated sandbox.
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
