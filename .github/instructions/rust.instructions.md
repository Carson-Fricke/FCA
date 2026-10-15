---
applyTo: "src/harness/**/*.rs,src/lake/**/*.rs"
---

# Rust Conventions

## Test placement (mandatory)

Never co-locate tests inside a source file. A `src/**/*.rs` file must not
contain a `#[cfg(test)] mod tests { ... }` block. Every test lives under the
package's own `tests/` directory — a peer of `src/` and `Cargo.toml` inside that
crate.

- **Integration tests (public API):** `tests/<name>.rs`. Cargo auto-discovers
  these as separate test binaries.
- **Unit tests that need private access:** `tests/unit/<name>.rs`, pulled into
  the owning source module with a path include:

  ```rust
  #[cfg(test)]
  #[path = "../tests/unit/<name>.rs"]
  mod tests;
  ```

  Adjust the `../` depth for nested modules (`src/a/b.rs` → `../../tests/unit/...`).
  Files under `tests/unit/` live in a subdirectory, so Cargo does **not**
  auto-discover them; they compile only through the `#[path]` include and keep
  access to private items via `use super::*`.

- Mirror the source module in the test file name, for example
  `src/backtest/engine_core.rs` → `tests/unit/backtest_engine_core.rs`.

## Workspace

- Workspace root is `src/harness/Cargo.toml`. Crates are direct children:
  `num`, `marketdata`, `backtest`, `live`, `api`, `mcp`, `config`,
  `contracts`, `strategy-sdk`, `strategy-jit`, `lakeclient`, `market-reader`. The embedded-Python host was removed: no
  harness build or test needs a Python interpreter, and authored programs run
  through the `harness-strategy-jit` seam.
- From `src/harness`, run `cargo fmt --all --check`,
  `cargo clippy --workspace --all-targets -- -D warnings`, and
  `cargo test --workspace`. No Python interpreter is needed.
- Workspace root is also `src/lake/Cargo.toml`. Crates are direct children:
  `contracts`, `configuration`, `catalog`, `coverage`, `providers`,
  `storage`, `ingestion`, `ingest-jobs`, `ingest-worker`, `query`,
  `http`, `local`, `reference`, `hosted`, `cli`. From `src/lake`,
  run the same three gates; no Python interpreter is needed.
