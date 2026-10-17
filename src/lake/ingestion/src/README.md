# src

Market acquisition transactions and staged partition writes over provider, writer, and committer ports. Option bars use provider OHLCV and terminal empty coverage, without trade derivation. HTTP and deployment composition belong to neighbors. Stored derivation inputs use the harness-owned verified reader.

- `publication.rs` — Validate immutable candidates and publish all coverage through the shared fenced manifest catalog.
- `bars.rs` — Session-aligned OHLCV bars: one range acquisition, per-session atomic publication, and exact trade derivation with half-open crypto UTC days.
- `common.rs` — Arrow partition-writer and atomic-committer ports, session-budgeted blocking completion with separate CPU and publication sections, canonical ordering, dataset selection, and transient retries.
- `columnar.rs` — Exact typed provider records projected directly into pinned Arrow columns, without JSON or decimal string conversions.
- `crypto.rs` — Atomic crypto pair-day ingestion.
- `equities.rs` — Atomic, dependency-injected ingestion for one equity symbol session.
- `ensure.rs` — Worker execution of accepted coverage gaps: exact datasets, bounded session concurrency, transient repair, and progress over persistence ports.
- `lib.rs` — Public ingest transactions, provider/writer/committer ports, result records, and exact trade derivation inputs.
- `providers.rs` — Provider boundaries for the equity, option, and crypto ingest transactions.
- `staging.rs` — In-memory staging writer over the schema-pinned Parquet serializer.
- `options/` contains the corresponding implementation slice.

The crate’s `examples/` folder contains the reproducible offline HTTP-to-publication replay benchmark.

- `trade_inputs.rs` projects verified Arrow batches to exact trade-domain records for bar derivation; it performs no storage reads or Parquet decoding.

Concurrent sessions own separate staging writers and retain session reservations through blocking completion. CPU-heavy validation, sorting, projection and encoding acquire the shared `lake-pipeline` budget; publication waits and writes own a separate I/O budget. No CPU slot is held across storage waits. Cancellation fences final visibility before job settlement.
