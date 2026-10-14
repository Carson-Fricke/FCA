# options

Option quote/trade ingestion and point-in-time individual-contract resolution. Historical quotes establish eligibility; historical option and underlying midpoints estimate delta using explicit model assumptions. This slice does not implement portfolio accounting or rolling.

- `backfill.rs` — Deterministic, resumable planning for option front-set backfills.
- `mod.rs` — Option ingestion: front-set selection, symbol-day transactions, backfill
- `resolve.rs` — Point-in-time contract resolution, including quote-implied model delta.
- `selector.rs` — Deterministic daily option front-set selection.
- `transaction.rs` — Dependency-injected option symbol-day ingestion.
- `worker.rs` — Queue controller handle for option ingestion.

`OptionController` is the canonical controller handle; there is no `OptionWorker` alias.
