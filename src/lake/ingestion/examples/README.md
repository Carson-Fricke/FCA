# Reproducible ingestion replay

- `replay.rs` runs an isolated loopback Alpaca service through the real HTTP decoder, typed provider, canonical Arrow/Parquet writer, local fenced manifest publisher and coverage reader. It checks quote/trade row counts and prints stage/resource snapshots with throughput.

No provider credentials, live market account or Azure services are used. Temporary objects are removed on exit. This benchmark includes the synthetic server's CPU and measures neither external ingress nor container startup. Commands and interpretation are in `docs/ingestion-performance.md`.
