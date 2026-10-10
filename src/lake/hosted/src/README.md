# Hosted lake composition

`lib.rs` exposes hosted runtime, API, and queue-worker composition.
`api.rs` builds the published HTTP router using Azure boundaries when storage
is configured. `runtime.rs` composes identity-authenticated catalog, jobs,
blob reads, SAS issuance, leases, and on-demand ingestion. `worker.rs`
composes the option-ingestion queue worker.

This folder composes neighboring lake capabilities; it does not own their
catalog models, HTTP routes, provider paging, or storage algorithms. ACA
startup selects the user-assigned client ID in the platform identity endpoint
before the pinned Rust credential reads it. Identity headers and access tokens
must remain in process memory.
