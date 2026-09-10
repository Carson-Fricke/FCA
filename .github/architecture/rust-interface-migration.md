# Rust Interface Migration

<!-- markdownlint-disable MD013 -->

## Status

The interface layers of the quant-harness are being ported to Rust crate by
crate. This document records the entrypoint flip, which is intentionally
partial: only the processes whose Rust implementations are complete are
flipped, and the Python engine remains authoritative for everything else.

## Ported entrypoints

| Console script | Rust binary | Rust crate | Status |
| --- | --- | --- | --- |
| `api` | `api` | `rust/crates/harness-api` | complete (axum HTTP boundary, bearer auth, provenance) |
| `mcp` | `mcp` | `rust/crates/harness-mcp` | complete (JSON-RPC dispatch, tool catalog, stdio transport) |
| `harness` | `harness` | `rust/crates/harness-api` | facade parsed in Rust; `api`/`mcp` served from Rust, `trader` pending |
| `backtest-job` | `backtest-job` | `rust/crates/harness-api` | surface parsed in Rust; engine injected seam pending |

Configuration is ported behavior-identically in `rust/crates/harness-config`
(loader, strict models, canonical hashing) and the strategy host surface in
`rust/crates/harness-host` (PyO3 strategy/indicator/screener hosts).

## Flip flag

Set `HARNESS_USE_RUST_ENTRYPOINTS` to `1`, `true`, `yes`, or `on` to make the
Python console scripts for the ported entrypoints exec the Rust binaries.

Binary discovery, in order:

1. `HARNESS_RUST_BINARY_DIR`.
2. `rust/target/release`.
3. `rust/target/debug`.

The flip is fail-safe: a disabled flag, an unported entrypoint, or a missing
binary returns control to the Python implementation. The dispatcher lives in
`src/harness/cli/rust_entrypoints.py`.

## Remaining before deleting the Python engine

The Python engine modules under `src/harness` are not deleted yet, and their
test suites remain as parity oracles, because:

* `backtest-job` and `trader` still run the Python engine through the injected
  `BacktestJobRunner` seam; the Rust backtest engine is not wired.
* The strategy context and binding bridge is a host surface only; engine-side
  strategy execution still runs through Python.
* The lint AST walker is bridged to the Python implementation rather than
  reimplemented in Rust.

Deletion of the Python engine paths is gated on those items and is tracked by
the decommission task.
