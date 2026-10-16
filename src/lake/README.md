# Market-data acquisition and control capabilities

- `Cargo.toml` declares the Rust capability workspace.
- `Cargo.lock` pins its dependencies.
- `rust-toolchain.toml` selects the Rust toolchain.
- `__init__.py` defines the retained Python value-library package.
- `__main__.py` delegates the Python command surface.

`ingest-jobs/` owns durable ACA execution control; `ingest-worker/` is the finite provider process. `hosted/` composes Azure adapters for the harness-hosted HTTP boundary in `http/`. Catalog/coverage, providers, ingestion transactions, writing/leases, reference acquisition and query policy remain separate capabilities. `local/` and `cli/` retain explicit owned-data cleanup. Reading/decoding and caches belong to `harness/market-reader/`.
