# src

Local filesystem composition, persistent coverage and option metadata, and on-demand ingestion. Option resolution seeds state/option-contracts.json for later requests and API restarts. Azure deployment composition belongs to lake-hosted.

- `application.rs` — Local-only composition for the market-lake HTTP application.
- `committer.rs` — Local commit boundaries: objects first, then coverage.
- `ensure.rs` — Filesystem and provider composition for the shared ensure application.
- `ingest.rs` — Alpaca ingestion composed over local filesystem adapters.
- `lib.rs` — Local composition over filesystem state: layout, ingest, backfill, and the ensure launcher.
- `options.rs` — Durable local option contract metadata.
- `state.rs` — Deterministic filesystem layout and local object URLs.
