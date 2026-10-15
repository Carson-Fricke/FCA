# Harness Rust workspace

- `Cargo.toml` declares engine, host, portable SDK and data-reader crates.
- `Cargo.lock` pins the workspace dependency graph.
- `rust-toolchain.toml` selects the Rust toolchain.

`market-reader/` owns read-only market-data verification and caching. `lakeclient/` composes availability, snapshots and signed reads. `api/` hosts execution/MCP and the Azure market-data routes. Engine, strategy SDK/JIT, live execution, market event projections, numeric and configuration capabilities remain separate siblings. See `.github/architecture/repository.md` for authoritative boundaries.
