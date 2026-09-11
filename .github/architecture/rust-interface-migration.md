# Rust Interface Migration

<!-- markdownlint-disable MD013 -->

## Status

The quant-harness engine core and the process interfaces are ported to the Rust
workspace under `src/harness/rust/`. The engine core — clock, event bus, replay,
matching/fills, fees, portfolio, risk, results, and the backtest runtime — is
native Rust. User-authored strategies, indicators, and screener programs remain
Python: the Rust engine drives them through the PyO3 plugin host in
`harness-host`. Strategies and indicators keep authoring against
`src/harness/strategy/**` and `src/harness/indicators/**`, and the example
strategies under `strategies/` are unchanged.

The native `backtest-job` runs the Rust engine end to end over the configured
`strategy_instances` and is parity-proven byte-for-byte against the Python
reference under the agreed definition (see below). The Zig native core was
retired in T253; `harness-native` builds the native library the Python ctypes
loader consumes.

## Ported entrypoints

| Process | Rust binary | Rust crate | Status |
| --- | --- | --- | --- |
| `api` | `api` | `src/harness/rust/crates/harness-api` | flipped (axum HTTP boundary, bearer auth, provenance) |
| `mcp` | `mcp` | `src/harness/rust/crates/harness-mcp` | flipped (JSON-RPC dispatch, tool catalog, stdio transport) |
| `harness` | `harness` | `src/harness/rust/crates/harness-api` | facade flipped for `api`, `mcp`, and `backtest-job`; `harness trader` stays on Python |
| `backtest-job` | `backtest-job` | `src/harness/rust/crates/harness-api` | flipped (native engine, parity-proven) |
| `trader` | `trader` | `src/harness/rust/crates/harness-live` | deferred; the Rust binary is a deterministic core demo, not the configured supervision runner |

Configuration is ported behavior-identically in
`src/harness/rust/crates/harness-config` (loader, strict models, canonical
hashing) and the strategy host surface in
`src/harness/rust/crates/harness-host` (PyO3 strategy/indicator/screener
hosts).

## Flip flag

Set `HARNESS_USE_RUST_ENTRYPOINTS` to `1`, `true`, `yes`, or `on` to make the
Python console scripts for the ported entrypoints exec the Rust binaries. The
`harness` facade flips only its ported process groups, so bare `harness` and
`harness --help` still render the Python help.

Binary discovery, in order:

1. `HARNESS_RUST_BINARY_DIR`.
2. `src/harness/rust/target/release`.
3. `src/harness/rust/target/debug`.

The flip is fail-safe: a disabled flag, an unported entrypoint, an unsupported
invocation, or a missing binary returns control to the Python implementation.
The dispatcher lives in `src/harness/cli/rust_entrypoints.py`.
`PORTED_ENTRYPOINTS` holds the verified set (`api`, `mcp`, `backtest-job`);
`DEFERRED_ENTRYPOINTS` records `trader` and why it stays on Python.
`backtest-job --data-dir` (the local-Parquet fixture) is also kept on Python
until the native engine wires a lake reader.

## Parity definition

Data-bearing artifacts are byte-identical across the two stacks except
`manifest.code_fingerprint`, which hashes each engine's own source by design.
The golden comparison in
`src/harness/rust/crates/harness-backtest/tests/parity.rs` masks that one field
in both payloads and newline-normalizes them before comparing.

## Remaining before deleting the Python engine

No engine module under `src/harness` has been removed yet, because active
Python code still imports the engine and the parity oracle depends on it:

* The retained Python strategy plugin surface is not yet decoupled from the
  engine. `harness.strategy.context` imports `harness.data`,
  `harness.portfolio`, `harness.venues`, and `harness.core`, so loading a user
  strategy on the Python path pulls in the engine the Rust host otherwise
  replaces. The protocol-only-imports refactor is a filed follow-up;
  `tests/harness/strategy/test_import_boundary.py` records the target state as
  an expected failure.
* `trader` has no Rust supervision runner, so the Python live runtime, OMS,
  fills, venues, portfolio, and risk modules it composes are still used.
* `harness.runtime.backtest` and the Python strategy context remain the oracle
  the golden parity fixtures were generated from.
* Active Python products (`thesis`, the sweeps/http/mcp surfaces) import the
  engine and results modules directly.

Under the decommission gate only modules with zero importers are deletable;
T279 removed the `harness.risk.engine` and `harness.risk.rules` compatibility
shims on that basis.
