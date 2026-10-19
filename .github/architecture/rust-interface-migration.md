# Rust Interface Migration

<!-- markdownlint-disable MD013 -->

## Status

The quant-harness engine core and the process interfaces are native Rust in the
workspace under `src/harness/`. The engine core — clock, event bus, replay,
matching/fills, fees, portfolio, risk, results, sweeps, and the backtest runtime
— is Rust, and the Python engine that used to mirror it has been deleted.

User-authored strategies, indicators, studies, and screening programs are
authored in Rust against `harness-strategy-sdk` and compiled and loaded on
submit through `harness-strategy-jit`. The embedded interpreter, the PyO3
strategy host, the `py` extension crate, and the harness Python packages were
deleted; the SDK exposes the authoring traits (strategy, indicator, study, and
screening programs) over the engine's event and context types, and the
versioned C ABI is the only contract that crosses a dylib boundary. A study or
screening program has no order surface by construction.

The native `backtest-job` runs the Rust engine end to end over the configured
`strategy_instances` and is parity-proven byte-for-byte against the Python
reference under the agreed definition (see below).

## Entrypoints

Every harness process is a Rust binary invoked directly; the Python
console-script shims were deleted with the harness Python packages, and the
runtime image puts the binaries on `PATH` under `/opt/rust/bin`.

| Entry point | Rust binary | Rust crate | Status |
| --- | --- | --- | --- |
| `harness` | `harness` | `api` | flipped; facade dispatches `api`, `mcp`, `backtest-job`, `study-job`, `screen-job`, and `trader` |
| `api` | `api` | `api` | flipped (axum HTTP boundary, bearer auth, provenance) |
| `harness-worker` | `harness-worker` | `api` | finite queued execution, dispatcher, isolated validation and durable smoke observer |
| `backtest-job` | `backtest-job` | `api` | flipped (native engine, parity-proven) |
| `screen-job` | `screen-job` | `api` | flipped (JIT-authored screening programs, `screen.json` schema `fca-harness-screen-v2`) |
| `trader` | `trader` | `live` | flipped (native supervision runner: config, sessions, feed supervision, reconciler, kill-switch, snapshots) |
| `harness mcp` | `harness` | `api` | flipped (JSON-RPC dispatch, tool catalog, stdio transport) |

Configuration is ported behavior-identically in `config` (loader,
strict models, canonical hashing); the authoring surface is the
`harness-strategy-sdk` / `harness-strategy-jit` pair. `harness-lakeclient`
coordinates acquisition and verified snapshot reads through injected native
ports in a combined host or authenticated routes for separate clients.

A missing binary fails with an actionable message; it never falls back to a
deleted Python implementation.

### Backtest market-data and execution boundaries

The complete harness runs locally or in Azure. Combined API and finite worker
composition inject native catalog/acquisition and snapshot-reading ports; HTTP
serves separate clients. `harness-market-reader` verifies immutable Parquet and
visits ordered Arrow batches, sharing snapshot/cache identity with the client.
There are no HTTP self-calls or synthetic-data fallbacks. Backtests always
acquire quotes/trades for execution in addition to consumer subscriptions;
studies acquire only the compiled subscription union.

Admission freezes authored source and input under the caller's retained job ID.
The API notifies finite workers; it runs no simulation or authored constructor.
Workers own durable claims, acquire missing data, report read/replay/publication
progress, publish result files and exit. API restarts do not lose work. Expired
worker ownership becomes interrupted, with no automatic replay. Ingestion uses
separate local/ACA workers and provider credentials; admission retries join one
retained ID. Ambiguous provider/publication/control outcomes are reconciled,
never inferred absent or blindly replayed. See
[market-data runtime](market-data-runtime.md) for resource and recovery contracts.

A closed session left `partial`, or an errored fetch, is a repairable gap rather than a terminal failure: the lake classifies both as actionable and re-fetches them inside the running ensure job, and the harness keeps polling while that job is in flight. Only a job that settles with coverage still short fails the ensure, naming the sessions the lake could not complete. A session that is still trading is accepted as partial.

## Parity definition

Data-bearing artifacts are byte-identical across the two stacks except
`manifest.code_fingerprint`, which hashes each engine's own source by design.
The golden comparison in
`src/harness/backtest/tests/parity.rs` masks that one field
in both payloads and newline-normalizes them before comparing.

## Python surface retained

No harness Python surface remains: the plugin host, the `py` extension crate,
and every harness Python package (`harness.configuration`, `harness.core`,
`harness.strategy`, `harness.indicators`, `harness.research`,
`harness.screening`, `harness.data`, `harness.testing`, and `harness.cli`)
were deleted in the native-authoring wave. The Python products that remain are
the lake service shim and its retained library surface (`src/lake`, see
`lake-rust-rewrite-plan.md`), Thesis (`src/thesis`), and its generic runtime
(`src/thesis_runtime`). The unused Python `lakeclient` SDK and its obsolete
soak driver are removed. Rust `harness-lakeclient` remains the active
market-data acquisition adapter.

Deleted engine modules: `harness.runtime`, `harness.oms`, `harness.fills`,
`harness.portfolio`, `harness.risk`, `harness.venues`, `harness.results`,
`harness.sweeps`, `harness.http`, `harness.mcp`, `harness.quant`,
`harness.signal`, `harness.strategy.context`, `harness.strategy.binding`, the
live/replay ingestion modules under `harness.data`, and the Python tests that
covered only those modules.
