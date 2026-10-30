# Prepared replay and execution hot paths - 2026-10-27

## Implemented scope

1. Removed historical-order work from tick expiration and managed targets.
   The venue uses deadline scheduling, per-symbol live indexes and a separate
   pending-fill index. Expiration waits for committed fills to book; simultaneous
   expirations retire indexes in one batch. Target reconciliation reads booked
   inventory directly; cancellation resolves the order symbol by ID. Terminal
   orders remain queryable as evidence.
2. Execution account, order and closed-trade JSON is generated only when an
   authored callback requests that view. Account caches belong to one immutable
   callback snapshot; unchanged immutable histories retain their lazy caches.
   Snapshot updates do not eagerly serialize histories. Explicit whole-history
   requests and result publication still necessarily process their output rows.
3. Historical ticks use bounded immutable blocks with exact scale-18 numeric
   coefficients, a shared symbol, arena-backed identifiers and deduplicated venue
   strings. Native matching, dispatch hashing and JIT callbacks consume borrowed
   views. No row strings are allocated when replaying block handles. Owned live
   and synthetic observations use the same market-view contract. Non-scale-18
   projection and null transforms retain the original checked numeric behavior.
4. Complete verified input streams publish reusable block files. Keys bind pinned
   snapshot inventory, symbol alias, exact window and decoder sources. Blocks
   have checked sizes, UTF-8 ranges, numeric bounds, chronological order and
   SHA-256 checksums; complete footers bind row totals. Incomplete writers never
   publish. Cold writer contention bypasses retention to avoid cross-input
   deadlock. Abandoned partials are reclaimed under their key's OS lock.

`HARNESS_PREPARED_REPLAY_DIR` defaults to `.local/prepared-replay` and
`HARNESS_PREPARED_REPLAY_BYTES` to 32 GiB. Zero disables disk retention while
retaining compact dispatch. Retained complete files are evicted oldest-first
under a short admission lock. Process memory remains bounded by cursor blocks,
not the complete date window. Different pinned inputs or exact windows get
different entries; overlapping windows are not yet a shared partition cache.
Cold preparation still decodes the original Arrow rows and adds storage work.

SDK ABI **7** replaces eager sink JSON with a callback-scoped execution query
source. Repository producers, consumers, tests and current guidance were updated
together. Older compiled artifacts fail the ABI handshake and rebuild under the
current compiler identity; there is no old-ABI reader. Historical job evidence
and market-lake data were preserved. Caches remain ignored local data.

## Production-boundary session comparison

All executions use cached **MSFT 2026-01-02**, one closed symbol-session:
696,291 trades, 288,813 quotes and 390 source bars, totaling **985,494 raw events**.
No provider ingestion, forward-window evaluation or full January-July rerun was
performed. Background applications were not stopped.

Three controlled authored programs share the same source and differ only by
parameters/subscriptions: bar-only, idle quote callbacks, and quote callbacks
that periodically change a managed signed target. Active runs produce **578
orders and 578 fills**. Accounting and identity remain harness-owned.

The baseline retains the deployed pre-change release API/worker, plus an isolated
SDK source snapshot with the execution-source changes reversed to ABI 6. The
optimized runner links the release native admission/finite-worker composition.
Each intentional execution retains its job ID and immutable input reference
before admission and validates its acknowledgement. Results use isolated storage.
No failed or unknown execution was automatically resubmitted.

Durable stage medians, in seconds:

| Case | Baseline replay | Warm prepared replay | Replay speedup | Baseline worker after compilation | New worker after compilation |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bars | 0.649895 | 0.448957 | 1.45x | 1.268128 | 1.158052 |
| Quotes | 1.937570 | 0.868162 | 2.23x | 2.654228 | 1.503247 |
| Active trading | 4.930256 | 1.360030 | 3.63x | 5.733683 | 2.215498 |

The corresponding worker improvements after compilation are **1.10x, 1.77x and
2.59x**. These are not a claim of 3x complete latency for every strategy.
Publication and admission remain significant for these short executions.

Replay samples:

- Bars baseline: 1.140792, 0.649895, 0.631218; warm: 0.448957, 0.696513, 0.446141.
- Quotes baseline: 1.937570, 2.030480, 1.848811; warm: 0.868162, 0.810348, 0.900737.
- Active baseline: 4.954130, 4.906382; warm: 1.335339, 1.432740, 1.360030.

The first optimized bar run, with no prepared entries, took **0.932935 s** in
replay. Preparing and writing blocks can make a first run slower; repeated
workers reuse the two complete files, totaling **92,602,082 bytes**. Initial
compilation is separately measured and excluded from the worker comparison.

Three baseline attempts hit the previously observed Windows `failed to persist
temporary file: Access is denied. (os error 5)`, including attempts through both
the isolated HTTP API and the finite worker without concurrent status reads.
They remain failed and are excluded from successful scientific comparisons.
This does not establish the OS-level cause. All ten optimized executions
succeeded in the initial comparison.

## Scientific equivalence

The comparator verifies every artifact's SHA-256, headline metrics, scientific
backtest fields, and every scientific Parquet schema and row. Only job identity
is normalized and the provenance manifest is excluded. Seventeen successful
baseline/warm executions compare exactly within their program case.
The first cold-preparation execution also matches the baseline scientific outcome
and dispatch hash after the same normalization.

Dispatch hashes:

- Bars and idle quotes: `469bb1e37d597812641587976ad61b900a40e02243dec81758a17f0a43d57b2d`.
- Active: `57cd4d9ff4a260dd43bcb820f1efab9cb1b2de18601e62fba17abe3361f840d7`.

Inputs, retained IDs, acknowledgements, job records, binaries and comparisons
are under ignored `.local/prepared-replay-2026-10-27/`. `compare.py` writes
`verified-comparison.json`; failed records are retained separately.

## Component measurements

Five optimized samples per case, excluding construction. The expiration baseline
is a controlled copy of the venue with its original historical scan restored;
all other probe code is shared. Timings include market observation and expiry.

| Historical filled orders | Original scan, ns/event | Deadline index, ns/event |
| --- | ---: | ---: |
| 0 | 18.640 | 21.334 |
| 600 | 628.619 | 23.194 |
| 3,000 | 4,885.432 | 23.011 |

Owned trade construction plus projection measures **198.0135 ns/event** and four
allocations; an existing block handle plus projection measures **16.671 ns/event**
and zero allocations. Block creation/loading is outside that microbenchmark;
complete replay and first-preparation costs are measured above. Components are
not added together to predict complete runtime. Raw samples are `micro-*.jsonl`.

## Verification

- Release API library build passed; isolated release entrypoints completed the
  production-boundary comparisons.
- Core package verification passed **184 tests**, including documentation tests,
  in **87.06 s**: `backtest`, `marketdata`, `harness-strategy-sdk`,
  `harness-trading`, and `harness-research`.
- Tests cover cold/warm reuse, changed source pins, corruption rejection, bounded
  retention, interrupted and concurrent producers, owned/block execution
  equivalence, managed targets, partial fills, cancellation, expiry, routing,
  causal indicators, null transforms and published accounting.
- The host lazy-view state-machine test passed. Authored SDK queries independently
  memoize each view and make no host query for unused account/history views.
- JIT compile/cache/ABI behavior, SDK identity, and API metrics passed together:
  **four tests**, **26.82 s** including process setup.
- The broader seven-package attempt reached the **180 s** limit in authored HTTP
  cases without an observed assertion failure. It was not rerun unchanged. The
  unfinished cases passed in bounded isolated selections: five authored cases
  in 12.06 s and two nested cases in 4.89 s, with overlap in those selections.
  API duplicate-admission/interruption/pool behavior also passed before timeout.
- Final review changed simultaneous deadline cleanup to retire a batch once;
  **13 focused execution/risk tests** passed afterward, including a new 256-order
  simultaneous-expiration case. The earlier 184-test run is not relabeled as a
  rerun of this final cleanup.
- Git whitespace and repository LF checks passed. No unrelated Python source,
  ignored credentials, caches or build artifacts were added to delivery.

The local harness was restarted with the current release build after confirming
that no execution jobs were active. Thesis, UI and PostgreSQL were left running.
Authenticated MCP `read_sdk_reference` reports ABI **7**, version `0.1.0` and SDK
identity `2446b079620cdd888b094ae5fad2ba6a7e61d5d32505aee2b1f6ac6eba7226f6`.
The restarted harness health endpoint returns `{"status":"ok"}`.
No commit or push was performed for this task.
