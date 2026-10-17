# Market acquisition transactions

- `Cargo.toml` declares the ingestion capability and its provider, storage, catalog and pipeline dependencies.
- `src/` owns shared acquisition transactions, exact column projection and immutable publication over injected ports.
- `tests/` verifies acquisition/publication behavior and the few pure numerical contracts.
- `examples/` contains the reproducible offline ingestion replay benchmark.

Deployment and job ownership belong to the finite worker and ingestion-job capabilities. Reading and decoding belong to the harness-owned market reader. Transaction stages share resource budgets without holding CPU slots across publication waits.
