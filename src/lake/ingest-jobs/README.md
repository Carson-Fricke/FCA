# Durable ingestion control

This crate owns durable ingestion admission and ACA execution control. It has no provider, Arrow, Parquet decoding or backtest dependency.

- `Cargo.toml` declares the catalog/blob/control-plane adapter dependencies.
- `src/` contains immutable work, persistence and reconciliation.
- `tests/` exercises real adapters against fake Azure HTTP.
