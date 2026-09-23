# Lake Rust Rewrite Plan

<!-- markdownlint-disable MD013 -->

## Status

Not started. This is the target shape and the porting order for replacing the
Python market-lake under `src/lake/` with a Rust product. It follows the pattern
of [Rust Interface Migration](rust-interface-migration.md): the service becomes
Rust, a thin Python surface survives for code that must keep importing values.

## Why rewrite

Three structural costs, measured on the local stack.

**1. Per-record Python normalisation, bound to one core by the GIL.** One
equity session is one full trading day of ticks. A single SPY session measured
407k-459k trades and 2.19M-2.78M quotes, so ~2.6-3.2M records each become a
Python dataclass, are sorted on a multi-key tuple, and are handed to `pyarrow`.
Observed completed-session throughput during a real backfill was 3 sessions in
27 minutes (~9-10 minutes per session) while burning slightly more than one
core: `asyncio` concurrency cannot parallelise CPU-bound normalisation. A
683-session window (2024-01-01..2026-09-22) is therefore a multi-day job, and
that window is exactly what an undeclared study window produced.

**2. The catalog is a whole-file JSON document.** 30,380 coverage rows in a
12 MB file. Every read re-parsed it (308 ms) and every write rewrote, fsynced,
and atomically replaced the whole file; under contention a coverage query cost
594 ms CPU and 2.5 s wall. A per-stamp parse cache and compact serialisation
have since been added, bringing a warm read to 20 ms, but the design is still
O(rows) per read and O(rows) per write, and every ensure polls coverage every
few seconds.

**3. The fetch path cannot spend the account's request budget.** Measured on
2026-09-23: the account's Alpaca plan permits 10,000 requests per minute, yet
one SPY-quote session (2,737,099 rows at the 10k page size means 274 pages)
took minutes, and a 39-session, four-series window took over 45 minutes with
SPY quotes as the final gate. Three concurrent ensure jobs each built a client
with `provider.request.max_concurrency: 4`, so about a dozen page requests
shared one key and 429 backoff (0.25s..30s) dominated the wall clock. The
design cannot spend the budget even in principle: Alpaca's page token chains
one page to the next, so pagination inside a single query is sequential, and
there is no process-wide requests-per-minute governor. The same window is
about 14,000 pages (10.7k of them SPY quotes): at the configured budget that
is under two minutes of requests, not three quarters of an hour.

Rust removes (1) outright — real parallelism and a streaming columnar parse
instead of per-record objects — removes (2) by replacing the document with an
indexed store, and removes (3) by driving one shared request-budget governor
over time-sliced, genuinely parallel fetches.

## Target shape

`src/lake/` becomes a Cargo workspace at the product root, exactly as
`src/harness/` is today: its `Cargo.toml` and the Python package `lake/`
coexist at one root, and the console script resolves through a thin Python shim
mirroring `harness.cli.rust_entrypoints`.

```text
src/lake/
  Cargo.toml     # workspace root
  contracts/     # asset classes, data types, sessions, partitions, Arrow schemas
  catalog/       # coverage records, catalog store, job store
  coverage/      # coverage calculation, gap planning, ensure application
  providers/     # Alpaca client: paging, retry/backoff, Retry-After
  storage/       # Arrow/Parquet writer, object store, atomic replace, leases, capacity
  ingestion/     # equity, crypto, and option transactions, record validation
  local/         # local layout, ensure launcher, local composition
  http/          # axum boundary: the published lake contract
  cli/           # `lake local api|ingest|reset`
```

Phase 5 adds `reference/`, `query/`, `queue/`, and `hosted/` (Azure).

Crate conventions follow `.github/instructions/rust.instructions.md`: unit tests
that need private access live in `tests/unit/<name>.rs` and are pulled in with
`#[path]`; integration tests are `tests/<name>.rs`. No inline
`#[cfg(test)] mod tests`.

## What already exists in Rust and should be hoisted, not rewritten

`src/harness/marketdata/` already ports most of the value layer:

* `lake_reader.rs` — reads and writes the exact
  `date=/asset_class=/data_type=/SYMBOL.parquet` Hive layout with the lake Arrow
  schemas (`write_lake_partition`).
* `catalog.rs` — ports the `lake.catalog.models` value objects (coverage,
  listings, option contracts).
* `lake.rs` — the synchronous lakeclient boundary, sessions, partition paths,
  and coverage response shapes.
* `replay.rs`, `options_replay.rs`, `gap_healing.rs` — partition readers and
  coverage-driven backfill.
* `src/harness/api/src/lake_ensure.rs` — a working ensure client (request,
  coverage polling, incompleteness classification) that becomes the lake's own
  ensure engine instead of a consumer of it.

The rewrite lifts these into `lake-contracts`, `lake-storage`, and
`lake-catalog`, and `marketdata` then depends on them — inverting today's
direction, where the harness owns the port.

## The public contract to preserve

The harness and Thesis consume the lake over HTTP and must not need changes:

* `POST /v1/catalog/ensure` — 202 with a `job_id`, or a coverage report.
* `GET /v1/jobs/{job_id}` — persisted ensure-job state.
* `GET /v1/catalog/coverage` — the entries the harness classifies.
* `GET /v1/catalog/files` — the file query that feeds replay.
* The query and reference routes (phase 5).

Two semantics are load-bearing and must be reproduced exactly:

1. **Coverage classification** — `complete | empty | partial | error |
   delisted | missing`, with `is_partial` derived from the session clock.
2. **Ensure terminality** — an open session is satisfiable while it trades; a
   closed session left `partial` or `error` is not satisfied, but it is a
   repairable gap: it fails an ensure only once the ensure job has settled.

Object layout, file naming, and Parquet schemas are frozen: the harness reads
them directly from disk.

## Porting order

**Phase 0 — fixtures and parity oracle (no product code).**
Record Alpaca responses for a fixed multi-session window covering the cases
that have actually broken: early close, empty session, closed-partial, error,
delisted, a crypto UTC day, and a print stamped exactly at the close. Freeze
the expected coverage JSON and the expected Parquet bytes per partition. This
is the acceptance oracle for every later phase.

**Phase 1 — `contracts`, `catalog`, `coverage`.**
Port the value objects, sessions, partitions, and schemas, hoisting
`marketdata`'s versions. Replace the JSON catalog with an indexed store, with a
one-time migration that reads the existing `catalog.json`. Exit: coverage
responses byte-identical to the oracle, warm coverage query well under 50 ms at
30k rows.

**Phase 2 — `providers`.**
Port the Alpaca client: cursor paging, the inclusive end bound, 10k page size,
429 `Retry-After`, bounded concurrency, and transient/permanent error
discrimination. Add two capabilities the Python client lacks: a configurable
`provider.request.requests_per_minute` budget with **one process-wide
token-bucket governor shared by every ensure job** (per-job semaphores multiply
the rate until 429s appear), and a time-sliced query planner: a session whose
rows exceed one Alpaca page is split into independent time slices fetched
concurrently and merged back in deterministic timestamp order, so a session's
274 pages are 274 parallel requests inside the budget instead of a sequential
chain. Exit: replays the phase-0 recordings exactly, including the paging and
retry edge cases, and a budget test proving a multi-session window sustains
the configured rate without 429-induced stalls.

**Phase 3 — `storage` and `ingestion` (equity).**
Streaming Arrow writer, per-session fetch/validate/sort/write with real
parallelism, atomic object writes, the capacity guard, and the committer
boundary. Exit: Parquet byte-identical to the oracle; throughput target met.

**Phase 4 — `local`, `http`, `cli`, flipped.**
Implement the published routes over the new store and ensure engine, flip the
`lake` console script to the Rust binary, and keep the Python shim for binary
discovery. Exit: the harness and Thesis run an end-to-end study against the
Rust lake with no changes on their side.

**Phase 5 — the remainder.** Crypto and option transactions, reference data,
DuckDB-backed query, the queue worker, leases, and the Azure-hosted boundaries.

**Phase 6 — delete the Python lake**, keeping only the surface below.

## The Python surface to retain

The Python package is a library to code that is not the lake service:

| Consumer | Imports |
| --- | --- |
| `scripts/soak/run_soak.py` | `lake.configuration.loader`, `lake.coverage.equity_sessions` |
| `scripts/experiments/spy_0dte/*` | `lake.contracts.*`, `lake.configuration`, `lake.providers.alpaca.*`, `lake.catalog.models` |
| `strategies/harness_examples/short_put_spread.py` | `lake.catalog.models.ContractRecord` |
| `tests/harness/data/*` | `lake.catalog.front_sets`, `lake.catalog.memory`, `lake.catalog.models` |

A thin `lake` package must therefore survive the service rewrite, exactly as
`harness.core` and `harness.data` survived the engine rewrite: the value objects
(`lake.contracts`, `lake.catalog.models`, the session helpers) and little else.
Everything service-shaped — `lake.local`, `lake.http`, `lake.ingestion`,
`lake.providers`, `lake.storage` — is deleted. `lakeclient` remains the
published client until its consumers migrate.

## Risks

* **Parquet byte-parity.** Arrow writer settings (compression, writer version,
  row-group sizing, field order) must match `pyarrow` exactly, or the harness's
  readers and the fingerprint expectations diverge.
* **Windows file semantics.** The atomic-replace retry and the shared class
  lock exist because a Windows replace is denied while any handle is open. The
  Rust store must provide equivalent serialisation and retry.
* **Ensure terminality, now agreed.** A closed session left `partial`, or an
  errored fetch, is a repairable gap: the lake re-fetches it inside the running
  job and the harness polls until that job settles, failing only then. The
  rewrite must preserve this agreement rather than reintroducing a fail-fast
  check on `partial`.
* **Catalog migration.** The rewrite must read the existing 30k-row JSON once
  and never regress local data.
* **Rate-limit stampede.** Today each ensure job builds its own client and
  semaphore, so concurrent jobs multiply the request rate until 429s appear.
  The Rust provider must own exactly one governor per process/account; job
  scheduling derives from it rather than each job racing the provider on its
  own. Retries must consume budget too, not bypass it.

## Acceptance criteria

* Coverage responses and Parquet partitions byte-identical to the phase-0
  oracle for the fixture window.
* Equity ingest throughput of at least 10 sessions per minute on the same
  hardware, provider permitting — roughly 100x the measured Python baseline of
  about 0.1 sessions per minute.
* Fetch throughput is budget-bound, not concurrency-bound: sustained request
  throughput of at least 70% of the configured `requests_per_minute` budget
  with 429-safe backoff, and the 2026-09-23 fixture window (39 sessions, four
  series, ~14k pages) fetched and ingested in under three minutes on the same
  hardware.
* Warm coverage query under 20 ms at 30k rows, with no whole-file re-parse.
* The harness and Thesis suites pass unchanged against the Rust lake.
* The `lake` console script and the retained Python imports still resolve.

## Out of scope

The harness's own lake-facing client is not rewritten here; it becomes a thin
consumer of the same contract. Alpaca credentials stay in the lake process. The
strategy and experiment scripts are not migrated to Rust.
