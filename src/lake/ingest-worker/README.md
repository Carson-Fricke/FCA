# Finite ingestion worker

One finite ACA execution processes one leased partition of a retained job. The worker owns provider credentials and ingestion; HTTP and backtest execution remain elsewhere. Its image carries no JIT toolchain or DuckDB.

- `Cargo.toml` defines the standalone worker binary and acquisition dependencies.
- `src/` contains the process entrypoint, identity adapter and work composition.
