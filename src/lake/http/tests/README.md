# Behavior verification

Tests exercise the public application and acquisition boundaries; fake transports replace external services. Pure numerical tests cover pricing arithmetic. Production implementation belongs to the sibling source folder.

- `ensure.rs` — Ensure and job-polling route behavior.
- `entra_auth.rs` — Entra managed-identity JWT authentication, ported from
- `health_auth.rs` — Health, authentication, and unmatched-route behavior.
- `query.rs` — File-query and deferred SQL-query route behavior.
- `read.rs` — Streaming Arrow IPC read route behavior, including bounded/coalesced frames, unchanged IPC bytes, final flush, and storage-error propagation.
- `reference.rs` — Point-in-time reference universe and facts route behavior.
- `support/` contains shared fixtures or focused boundary checks.
