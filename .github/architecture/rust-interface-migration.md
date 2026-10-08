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
| `backtest-job` | `backtest-job` | `api` | flipped (native engine, parity-proven) |
| `screen-job` | `screen-job` | `api` | flipped (JIT-authored screening programs, `screen.json` schema `fca-harness-screen-v2`) |
| `trader` | `trader` | `live` | flipped (native supervision runner: config, sessions, feed supervision, reconciler, kill-switch, snapshots) |
| `harness mcp` | `harness` | `api` | flipped (JSON-RPC dispatch, tool catalog, stdio transport) |

Configuration is ported behavior-identically in `config` (loader,
strict models, canonical hashing); the authoring surface is the
`harness-strategy-sdk` / `harness-strategy-jit` pair, and `harness-lakeclient`
carries every market-data read over the lake routes.

A missing binary fails with an actionable message; it never falls back to a
deleted Python implementation.

### Backtest lake data (HTTP routes)

The harness has no local lake path: `--data-dir`, `HARNESS_DATA_DIR`,
`LAKE_LOCAL_DATA_DIR`, `.local/market-lake`, and `marketdata::LocalLakeReader`
were deleted. Every market-data read streams from the market-lake HTTP routes
through `harness-lakeclient` (async `reqwest`, with a blocking facade for the
JIT path): ensure, job status, and coverage for the run window; `GET /v1/read`
for the Arrow IPC stream of trades, quotes, and bars (bars select their
`interval`); and `GET /v1/reference/universe` / `GET /v1/reference/facts` for
screening context. The lake endpoint resolves from `LAKE_API_URL` (then
`LAKE_API_BASE_URL`) and `LAKE_API__BEARER_TOKEN`; a missing or rejected lake
configuration fails the run loudly and never falls back to local files or a
synthetic fixture.

Before a run, the harness API asks the market-lake ensure contract (`LAKE_API_URL`, `LAKE_API__BEARER_TOKEN`) to fetch every configured symbol/data-type for the run window, then polls coverage until every session is terminal. One request covers the whole window: the lake plans, resumes, and retries its own fetches server-side, so callers never split ranges and a repeat request continues an interrupted ensure. Coverage polling has no overall deadline and backs off to a fixed ceiling; it ends only on a lake-reported failure. The lake pages each fetch until the provider cursor is exhausted, so a closed session is complete rather than truncated. The lake owns provider credentials and the fetch; the harness never reads them. A symbol set the lake has not ingested before is thus fetched on demand rather than failing preflight with a missing-symbol error.

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
`lake-rust-rewrite-plan.md`), the consumer-facing `lakeclient` client
(`src/lakeclient`), and Thesis (`src/thesis`).

Deleted engine modules: `harness.runtime`, `harness.oms`, `harness.fills`,
`harness.portfolio`, `harness.risk`, `harness.venues`, `harness.results`,
`harness.sweeps`, `harness.http`, `harness.mcp`, `harness.quant`,
`harness.signal`, `harness.strategy.context`, `harness.strategy.binding`, the
live/replay ingestion modules under `harness.data`, and the Python tests that
covered only those modules.
