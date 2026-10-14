# Hosted lake composition

`lib.rs` exposes hosted runtime, API, and queue-worker composition.
`api.rs` builds the published HTTP router using Azure boundaries when storage
is configured. `runtime.rs` composes identity-authenticated catalog, jobs,
blob reads, SAS issuance, leases, and on-demand ingestion. `worker.rs`
composes the option-ingestion queue worker.

Local and hosted ensure launchers delegate to `lake_ingestion::EnsureIngest`.
Both execute the accepted gap plan, request exactly one dataset, use four
concurrent tick sessions with independent staging buffers, and leave durable
job settlement to coverage application. Bars prefer provider OHLCV and may
derive from already stored trades; they never prefetch ticks. Hosted ensure
also supports crypto UTC days and publishes session progress.

This folder composes neighboring lake capabilities; it does not own their
catalog models, HTTP routes, provider paging, or storage algorithms. ACA
startup selects the user-assigned client ID in the platform identity endpoint
before the pinned Rust credential reads it. Identity headers and access tokens
must remain in process memory.

Azure API and worker composition. Option resolution uses the contract repository in the coverage table; option quote/trade transactions use leased commits and option bars use provider OHLCV. Shared selectors and pricing policy belong to contracts and ingestion.
