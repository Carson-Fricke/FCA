# src

Provider-independent market values, sessions, partition identities, Arrow schemas, and individual-option selectors. It serves ingestion and consumers without depending on HTTP or providers.

- `decimal.rs` — Scale-18 fixed-point decimal value.
- `error.rs` — Shared data-layer failures, retaining remote HTTP status and service code for storage recovery without credentials or response bodies.
- `lib.rs` — Stable market-lake contracts.
- `options.rs` — Individual option selectors shared by the lake and research clients.
- `partitions.rs` — Asset classes, data types, logical Hive paths and strict immutable SHA-256 object references.
- `schemas.rs` — Versioned Arrow schemas for persisted trades, quotes, and OHLCV bars, including individual options.
- `sessions.rs` — Exchange and UTC-day session calendars.
- `symbol.rs` — Canonical market-symbol validation.
- `time.rs` — Minimal, dependency-free UTC calendar primitives.
