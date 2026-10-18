# Storage behavior checks

- `parquet_oracle.rs` validates canonical schema and row decoding against stored Parquet fixtures, deterministic current-batch writes with exact byte/checksum metadata, and schema/partition mismatch rejection.
- `oracle/` contains those fixtures and their manifest.
- `unit/` hosts Azure adapter behavior tests requiring private SDK fixture construction. They exercise the real lease facade and SDK over a fake external HTTP service, without adding production test hooks.

- `local.rs` verifies that reserved transport staging is unaddressable and concurrent byte/file replacements expose only committed object names.
