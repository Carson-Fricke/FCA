# Behavior verification

Tests exercise the public application and acquisition boundaries; fake transports replace external services. Pure numerical tests cover pricing arithmetic. Production implementation belongs to the sibling source folder.

- `ensure.rs` — Ensure flow parity tests with injected fake launchers.
- `catalog_queries.rs` — Coverage classification through the Azure adapter targets selected symbol/dataset partitions and bars source revisions, never the full catalog.
