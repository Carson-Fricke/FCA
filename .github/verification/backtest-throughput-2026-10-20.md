# Backtest throughput and catalog preflight verification

This record covers the local implementation and verification of catalog-based
preflight, exact result-pass fusion, typed Arrow encoding, accelerated hashing,
cached routing and separate result/publication progress. It does not record an
Azure rollout or a new six-month backtest benchmark.

## Behavior

- Availability and immutable provenance come from committed catalog snapshots.
  Preflight makes no quote/trade object requests. Bars needed for completed
  consumer frames are read before replay; ticks are read once during replay.
- The shared reader still checks object existence, checksums, schemas and
  physical row counts. A fake external deletion after catalog pinning fails
  replay. Partial-day runs finalize observed counts; full-day runs additionally
  check catalog totals.
- Catalog replay explicitly has no materialized row fingerprint. Its input
  identity includes pinned object manifests, symbol mapping and exact window.
  Null replay derives deterministic transform provenance without a tick scan.
- Ledger construction also computes first-pass statistics and bounded chart
  points. The single subsequent ledger reader supplies exact ordered variance
  and Parquet encoding. Every valuation sample remains present.
- Numeric and textual recorder inputs share typed Arrow builders and the
  current schema. Canonical decimal formatting, ids, sequences and Parquet
  properties are preserved. Hashing uses the standard accelerated SHA-256
  implementation. Routing subscriptions are frozen once and frame ordering
  compares borrowed keys.
- Computing results, writing artifacts and publishing artifacts are distinct
  stages. Row counters advance during generation and upload bytes advance
  during publication. Successful terminal commits complete observed stages;
  the artifact inventory and metrics remain atomically visible after upload.

## Optimized local component comparison

The same machine and one-million-sample synthetic workload were used before
and after. These are component measurements, not end-to-end Azure estimates.

| Component | Before | After | Ratio |
| --- | ---: | ---: | ---: |
| Encode 1M equity samples | 7.078 s | 1.186 s | 5.97x |
| Exact two-pass metrics, mostly flat equity | 2.306 s | 0.092 s | 24.94x |
| SHA-256 over 64 MiB | 0.221 s | 0.035 s | 6.40x |
| Generate 1M deterministic ids | 0.642 s | 0.398 s | 1.61x |

The original and new equity artifacts are **byte-identical**, both 19,776,402
bytes with SHA-256
`97f52a288d23acec6aaf46764e14fd67feb93bd464a56f2e0ce4c2bfb89db481`.
An executable behavior test also compares numeric and textual recording across
multiple flush boundaries with changing balances.

## Verification and limits

- `uv lock --check`, repository Ruff lint/format and ty passed.
- Harness workspace formatting and Clippy with warnings denied passed.
- The initial native integration build encountered a stale DuckDB Rust-wrapper
  cache. `cargo clean -p duckdb --manifest-path src/harness/Cargo.toml` rebuilt
  only that wrapper, preserving the expensive native library. Focused execution
  and SDK-identity tests then passed.
- The one native workspace retry ran **330 tests successfully**, with no failed
  cases, before its 300-second runner deadline at documentation tests. A
  separate `cargo test --workspace --doc --manifest-path src/harness/Cargo.toml`
  passed, including the SDK compile-fail documentation test. No third full
  native test run was performed.
- Lake workspace formatting and tests passed.
- UI checking found no errors and two existing CSS warnings in
  `SketchToolCard.svelte`. All eight execution-card tests passed, including
  separate computation/writing/publication labels and uploaded-byte display.
- The full Python run exceeded the 180-second budget. Its Windows child
  continued after the launcher timed out and finished with **1,665 passed,
  one explicitly disabled live-Cosmos test skipped**, in **239.68 seconds**.
  The test at the timeout boundary and the mandatory delivery architecture
  guards were isolated under a hard timeout: **77 passed in 22.78 seconds**.
  There was no hang in that isolated set. The full Python budget remains an
  outstanding repository verification finding; no unrelated test trimming or
  unchanged full-suite rerun was performed.

Detailed local job records, profiles, hashes and logs are retained under the
ignored `.local/verification/backtest-artifacts-2026-10-20/` directory. No
production execution was cancelled or replayed, and no catalog data was reset.
