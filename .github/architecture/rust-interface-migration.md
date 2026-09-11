# Rust Interface Migration

<!-- markdownlint-disable MD013 -->

## Status

The interface layers of the quant-harness are being ported to Rust crate by
crate. This document records the entrypoint flip, which is intentionally
partial: only the processes whose Rust implementations are complete are
flipped, and the Python engine remains authoritative for everything else. The
Zig native core was retired in T253; the native library the Python loader
consumes is now built from the Rust `harness-native` crate.

## Ported entrypoints

| Process | Rust binary | Rust crate | Status |
| --- | --- | --- | --- |
| `api` | `api` | `rust/crates/harness-api` | flipped (axum HTTP boundary, bearer auth, provenance) |
| `mcp` | `mcp` | `rust/crates/harness-mcp` | flipped (JSON-RPC dispatch, tool catalog, stdio transport) |
| `harness` | `harness` | `rust/crates/harness-api` | facade flipped for its ported groups only: `harness api` and `harness mcp` exec the Rust binary; `harness backtest-job` and `harness trader` stay on Python |
| `backtest-job` | `backtest-job` | `rust/crates/harness-api` | wired to the flip switch but gated off; the Rust engine is not wired to the command and no cross-stack parity exists |
| `trader` | `trader` | `rust/crates/harness-api` | wired to the flip switch but gated off; the Rust live supervision runner is not ported |

Configuration is ported behavior-identically in `rust/crates/harness-config`
(loader, strict models, canonical hashing) and the strategy host surface in
`rust/crates/harness-host` (PyO3 strategy/indicator/screener hosts).

## Flip flag

Set `HARNESS_USE_RUST_ENTRYPOINTS` to `1`, `true`, `yes`, or `on` to make the
Python console scripts for the ported entrypoints exec the Rust binaries. The
`harness` facade flips only its ported process groups, so bare `harness` and
`harness --help` still render the Python help.

Binary discovery, in order:

1. `HARNESS_RUST_BINARY_DIR`.
2. `rust/target/release`.
3. `rust/target/debug`.

The flip is fail-safe: a disabled flag, an unported entrypoint, or a missing
binary returns control to the Python implementation. The dispatcher lives in
`src/harness/cli/rust_entrypoints.py`. `PORTED_ENTRYPOINTS` holds the verified
set (`api`, `mcp`); `DEFERRED_ENTRYPOINTS` records the gated entrypoints and why
they stay on Python.

## Remaining before deleting the Python engine

The Python engine modules under `src/harness` are not deleted yet, and their
test suites remain as parity oracles, because:

* `backtest-job` still runs the Python engine. The `harness-api` binary drives
  the `UnportedBacktestJobRunner` seam; the Rust backtest job in
  `harness-backtest` runs a built-in deterministic strategy/risk/fill seam and
  does not load the configured `strategy_instances` through the strategy host.
* No Rust `backtest-job` artifact is parity-proven against Python. The
  committed benchmark configuration must produce byte-identical `backtest.json`
  on both stacks; no cross-stack replay test exists yet.
* `trader` has no Rust supervision runner. The Rust facade reports the `trader`
  group as not yet ported, and `harness-live` is not wired to the CLI.
* Engine-side strategy execution still runs through the Python context and
  binding rather than the Rust strategy host.

Deletion of the Python engine paths is gated on those items and is tracked by
the decommission task. The precise gate lives in `repository.md` under
"Decommission Status".
