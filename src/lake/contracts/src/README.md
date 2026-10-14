# src

Provider-independent market values, sessions, partition identities, Arrow schemas, and individual-option selectors. It serves ingestion and consumers without depending on HTTP or providers.

- `decimal.rs` — Scale-18 fixed-point decimal value.
- `error.rs` — Shared data-layer failure type.
- `lib.rs` — Stable market-lake contracts.
- `options.rs` — Individual option selectors shared by the lake and research clients.
- `partitions.rs` — Asset classes, data types, and the frozen Hive partition layout.
- `schemas.rs` — Versioned Arrow schemas for persisted trades, quotes, and OHLCV bars, including individual options.
- `sessions.rs` — Exchange and UTC-day session calendars.
- `symbol.rs` — Canonical market-symbol validation.
- `time.rs` — Minimal, dependency-free UTC calendar primitives.
