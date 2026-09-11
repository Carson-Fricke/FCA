---
applyTo: "src/harness/**/*.rs"
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
  `native`, `num`, `marketdata`, `backtest`, `live`, `api`, `mcp`, `host`,
  `config`, `py`.
- From `src/harness`, run `cargo fmt --all --check`,
  `cargo clippy --workspace --all-targets -- -D warnings`, and
  `cargo test --workspace`. The strategy host embeds CPython, so set
  `PYO3_PYTHON` and put the interpreter's directory on `PATH` when running tests.
