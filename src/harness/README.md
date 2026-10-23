# Harness Rust workspace

- `Cargo.toml` declares engine, host, portable SDK and data-reader crates.
- `Cargo.lock` pins the workspace dependency graph.
- `rust-toolchain.toml` selects the Rust toolchain.

`market-reader/` owns read-only market-data verification and caching. `lakeclient/` composes availability, snapshots and signed reads. `api/` hosts execution/MCP and the Azure market-data routes. Engine, strategy SDK/JIT, live execution, market event projections, numeric and configuration capabilities remain separate siblings. See `.github/architecture/repository.md` for authoritative boundaries.

- `backtest/`: Deterministic replay, subscription planning and result computation.

- `config/`: Current configuration schema and strict decoding.

- `contracts/`: Neutral wire values, canonicalization and hashing.

- `execution/`: Neutral admission, job lifecycle, ownership and persistence ports.

- `execution-storage/`: Conditional filesystem and memory adapters for the execution lifecycle.

- `live/`: Paper/live runtime hosts, supervision and venue adapters.

- `marketdata/`: Typed market events and availability contracts.

- `mcp/`: MCP schemas, injected operation handlers and protocol dispatch.

- `num/`: Exact numeric representations and checked arithmetic.

- `research/`: Configuration, acquisition, authored-program composition and artifact publication.

- `strategy-jit/`: Native authored-program compilation, loading and adaptation.

- `strategy-sdk/`: Authored-program observation and intent contracts.

- `trading/`: Shared deterministic matching, accounting, fee and risk policy.
