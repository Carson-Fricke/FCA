# options

Option quote/trade ingestion and point-in-time individual-contract resolution. Historical quotes establish eligibility; historical option and underlying midpoints estimate delta using explicit model assumptions. This slice does not implement portfolio accounting or rolling.

- `backfill.rs` — Deterministic, resumable planning for option front-set backfills.
- `mod.rs` — Public facade for selection, planning and symbol-day transactions.
- `resolve.rs` — Point-in-time contract resolution, including quote-implied model delta.
- `selector.rs` — Deterministic daily option front-set selection.
- `transaction.rs` — Dependency-injected option symbol-day ingestion.

`lake-ingest-worker` owns execution and durable ownership. This slice has no
queue consumer or alternative ingestion entrypoint.
