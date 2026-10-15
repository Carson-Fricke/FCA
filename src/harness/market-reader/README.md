# Harness market-data reader

A read-only service shared by native harness execution, HTTP read encoding and ingestion derivation. Provider acquisition, availability, job dispatch and execution policy belong to callers.

- `Cargo.toml` defines the value-contract/Arrow/Parquet dependency boundary.
- `src/` contains snapshot validation, bounded caches and batch reads.
- `tests/` verifies integrity and reuse against canonical Parquet.
