# Bounded result encoding

This folder owns exact result schemas, deterministic identifiers and Parquet
encoding. It receives results; it does not read market data, reconstruct trades,
admit jobs or publish remote objects.

- `mod.rs`: exposes recorder contracts and implementations.
- `config.rs`: validates recording configuration and metadata defaults.
- `artifacts.rs`: constructs owned artifact paths.
- `columns.rs`: appends borrowed cells and exact decimals directly into typed Arrow builders; equal decimal values reuse canonical formatting.
- `ids.rs`: generates reproducible identifiers with cached seed framing and accelerated SHA-256.
- `manifest.rs`: hashes files and creates the final inventory and run digest.
- `parquet.rs`: flushes bounded column batches into the current schema and writer properties.
- `run.rs`: orders records and ids; numeric portfolio samples and general textual records converge on the same Arrow builders.
- `schemas.rs`: defines the single current result schema.
- `writer.rs`: validates general rows and provides canonical reference JSON output.

Numeric portfolio recording preserves every sample, field, null, sequence and id.
It removes per-row maps, owned row copies and repeated decimal formatting. The
result schema and Parquet batch boundaries remain the same for both callers.
