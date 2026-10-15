# src

Market acquisition transactions and staged partition writes over provider, writer, and committer ports. Option bars use provider OHLCV and terminal empty coverage, without trade derivation. HTTP and deployment composition belong to neighbors. Stored derivation inputs use the harness-owned verified reader.

- `publication.rs` — Azure composition for the provider-backed ingest transactions.
- `bars.rs` — Session-aligned OHLCV bars: provider-first with exact trade derivation.
- `common.rs` — Shared ports, data-type normalization, and retry helpers for atomic ingest.
- `crypto.rs` — Atomic crypto pair-day ingestion.
- `equities.rs` — Atomic, dependency-injected ingestion for one equity symbol session.
- `ensure.rs` — Worker execution of accepted coverage gaps: exact datasets, bounded session concurrency, transient repair, and progress over persistence ports.
- `leased.rs` — Lease-protected atomic ingest composition.
- `lib.rs` — Equity, option, and crypto ingest transactions, record validation, and the
- `providers.rs` — Provider boundaries for the equity, option, and crypto ingest transactions.
- `staging.rs` — In-memory staging writer over the schema-pinned Parquet serializer.
- `options/` contains the corresponding implementation slice.

- `trade_inputs.rs` projects verified Arrow batches to exact trade-domain records for bar derivation; it performs no storage reads or Parquet decoding.
