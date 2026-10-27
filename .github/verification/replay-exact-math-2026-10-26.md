# Exact arithmetic and replay throughput - 2026-10-26

## Scope and preserved behavior

This follow-up implements the recommended arithmetic and result-computation
optimizations, plus bounded merge/dispatch and allocation improvements. The
first-round evidence remains in `replay-hot-path-2026-10-26.md` unchanged.

- Scale-18 multiplication and ratios short-circuit exact identities, use narrow
  products when possible and cancel common factors before widening. Wide division
  visits only possible quotient bits instead of always visiting all 256 bits.
- Variable-scale arithmetic returns already-exact narrow results directly,
  preserving coefficients, signed zero, exponents and context precision without
  allocating digit vectors or formatting integers.
- Valuation summaries calculate each changing-equity return once, retaining a
  bounded compressed cache with one entry per financial-state row. The variance
  pass reads that cache, retaining exact decimal rounding, sample weighting and
  the original ordered floating-point additions, including zero-return runs.
- Scratch histories reuse compression/decompression workspaces and exclusive
  write handles. Clones still fork before append and readers retain their exact
  snapshots. Scratch is execution-local; no persisted format changed.
- Lake cursors use a fixed tournament over at most 32 heads. The event bus keeps
  one external head alongside generated/backlogged events with the same complete
  ordering key and feed ordinal. Existing symbol-keyed marks update in place.

No market events are dropped, no fills or prices are approximated, and no
financial arithmetic is replaced by floating point. Admission, retained job
identity, immutable source pins and publication contracts are unchanged.

## Profiling and measurement

System-wide Windows CPU recording was unavailable because Windows rejected its
performance-policy request. A disposable process-local instruction-pointer sampler
was used instead, with eager symbol loading and release line tables for selected
Rust crates. Only the isolated benchmark process was sampled. The two earlier
attempts without usable Rust symbol resolution are excluded.

The successful Jan-Feb continuation profile collected 8,039 samples. Resolved
Rust symbols include `decimal18::div_round` (729), `Decimal18::checked_mul`
(270), variable-scale `u256_to_digits` (361), event dispatch (184), Arrow row
decoding (136) and event projection (102). Allocation functions also appear.
These are instruction-pointer samples, not inclusive stack percentages or an
additive wall-time breakdown. Profile runs include sampler overhead and are not
used as throughput measurements.

The matching after-profile collected 4,365 samples with no failed context reads.
Wide decimal division and digit conversion no longer dominate resolved Rust
samples; Arrow row decoding, column access, allocation and canonical dispatch
remain visible. The same Jan-Feb program/window succeeds with identical
scientific outcomes under the benchmark metadata normalization.

All measurements use the AMD Ryzen 9 5900HX workstation and existing optimized
release settings. Selected crates include line tables for profiling; optimization
levels are unchanged. Background applications were not stopped. Full-window
comparisons against first-round runs remain observational, not idle-host A/Bs.

Benchmark entrypoints link the real native submitter, durable admission and
finite execution worker. Each new execution retains its own job ID before
dispatch and uses isolated owned results. Cached Jan-Jul MSFT inputs are reused;
no provider ingestion or forward-window evaluation is introduced. Running app
processes and their loaded binaries are not replaced by these measurements.

Raw configuration references, logs, sampler source/output, comparators and
microbenchmark helpers are in ignored `.local/replay-performance/`.

## Completed continuation comparison

Both runs consume 170,466,873 raw market events and produce 612 orders and fills.
Every scientific `backtest.json` field and headline metric compares exactly after
normalizing only execution identity and omitting manifest configuration/code
fingerprints. The dispatch hash remains
`a3085750bf691fbf0f6aa870b44ffd7d67444c3f7b22c7c8b223d5cd304af149`.

| Stage | First-round optimized | This round |
| --- | ---: | ---: |
| Replay | 293.244 s | 161.914 s |
| Result computation | 84.306 s | 7.595 s |
| Worker through publication | 379.482 s | 175.688 s |
| Caller wall | 379.760 s | 175.784 s |

This is a further 2.16x complete-worker speedup, and 7.04x relative to the
original retained 1,237.575-second run. One minute has not been reached.
The worker total includes a 4.366-second compilation stage in this run.

First-round job: `job-perf-64991368b03647d5acd4368f15e92b80`.
This-round job: `job-perf-f2c0f0da86d7448c8f70a3e6971a0e0b`.

The one-session continuation run also retains identical scientific outcomes,
metrics and dispatch hash. Its initial compilation dominates caller wall time,
so that total is not used as a replay-speed comparison.

## Completed reclaim comparison

The successful separate validation consumes the same 170,466,873 raw events and
produces the same 168 orders and fills. All scientific outcome fields and
headline metrics compare exactly under the same execution-metadata normalization.
Dispatch hash:
`4abd71372222143f77ef49f0adce3baae5d7be37e1bf2c176981eabdeaa140a7`.

| Stage | First-round optimized | This round |
| --- | ---: | ---: |
| Replay | 266.011 s | 157.201 s |
| Result computation | 19.390 s | 1.889 s |
| Worker through publication | 287.120 s | 160.818 s |
| Caller wall | 287.536 s | 160.923 s |

This is a further 1.79x complete-worker speedup, and 6.52x relative to the
original retained 1,048.300-second run.
First-round job: `job-perf-6830bd02ee0648e19fd6608372c78858`.
This-round job: `job-perf-ddbbde2f140a4ced87ea023dd5d70942`.

For both full jobs, every scientific Parquet table also compares schema and row
values exactly after normalizing execution IDs: actions, emissions, equity,
events, execution reports, fills, markers, orders, position transitions and risk
evidence. The equity tables retain 54,826 continuation and 13,950 reclaim rows.
The provenance manifest is excluded from this row comparison because execution
and code identities intentionally differ. Raw comparison results are retained
as `*-parquet-comparison.json` under the benchmark directory.

## Repeated release microbenchmarks

Three paired runs, sequential before/after binaries, with no compiler, full
backtest or sampler running concurrently. Medians in nanoseconds per operation:

| Component | First-round implementation | This round | Speedup |
| --- | ---: | ---: | ---: |
| One-share scale-18 valuation multiplication | 864.89 | 8.39 | 103.09x |
| Representative scale-18 equity ratio | 849.77 | 46.22 | 18.39x |
| Full-precision scale-18 ratio | 841.63 | 15.01 | 56.07x |
| Fractional scale-18 multiplication | 887.26 | 30.02 | 29.56x |
| Variable-scale quote midpoint | 217.7 | 41.9 | 5.20x |
| Enqueue, dispatch and canonical hashing | 275.7 | 241.4 | 1.14x |
| Resident synthetic Arrow trade decode, no I/O | 351.1 | 345.3 | 1.02x |
| Fixture-to-engine trade projection | 195.1 | 189.7 | 1.03x |

The unchanged venue controls remain approximately 15-18 ns/event. Arrow decode
and projection changes in this paired measurement are within ordinary variation,
not claimed as new improvements. The constant half-spread diagnostic also speeds
up, but it was already removed from the production per-event path in round one.
These costs are isolated and must not be added into an end-to-end prediction.
Raw samples and medians: `.local/replay-performance/round2-micro-summary.json`.

## Failed measurement retained

Reclaim execution `job-perf-27f1cb2c296248ce84a28c6b264ef9e6` reached 170,000,384
events and then failed with `failed to persist temporary file: Access is denied.
(os error 5)` while saving execution progress. It has no published artifacts and
is excluded from successful performance comparisons. The terminal failure was
reconciled before intentionally starting a separate recorded validation execution;
no terminal record was overwritten and no unknown-outcome mutation was replayed.

## Remaining opportunities

This round does not introduce a new compact event representation with integer
instrument IDs, a shared decoded-input cache across independent jobs, or parallel
strategy-variant execution. Those are separate remaining throughput opportunities;
their gains are not included in the numbers above. Stateful replay is not split
across dates. The running app still needs a coordinated binary restart to use
the optimized implementation.

## Verification

After production edits froze:

- `uv lock --check`: passed.
- `uv run --extra dev ruff check .`: passed.
- `uv run --extra dev ruff format --check .`: passed, 828 files formatted.
- `uv run --extra dev ty check`: passed.
- `node scripts/ci/line-endings.mjs --check`: passed.
- `git diff --check`: passed.
- The full Python suite reached the 180-second bound with no assertion failure
  observed; its active module was `test_model_output_streaming.py`.
- The full Rust workspace reached the 180-second bound during compilation,
  including DuckDB/lake-query. Neither unchanged complete suite was rerun.
- Hard-bounded Python isolation of that active module, HTTP harness
  acknowledgements, the mandatory delivery architecture guard and submission
  interruption/recovery passed: 100 tests, 24.41 seconds of pytest time,
  30.96 seconds including process setup.
- `cargo test --manifest-path src/harness/Cargo.toml -p num -p backtest -p marketdata -p harness-research`
  passed 157 tests, 53.91 seconds including compilation and doc-test setup.
  This includes the independent exact-ratio oracle, all rounding modes and
  overflow boundaries, signed-zero/exponent preservation, cached versus dense
  metrics through spills/flat runs/undefined means, snapshot isolation, and
  single/odd/maximum cursor ordering tests.
- `cargo test --manifest-path src/harness/Cargo.toml -p api --lib --test execution_pool`
  passed 12 tests, including acknowledgement validation, interrupted admission,
  fencing and retained execution identity; 42.71 seconds including compilation.
- `cargo test --manifest-path src/harness/Cargo.toml -p api --test http_parity duplicate_submission_joins_and_conflicting_input_fails_closed -- --exact`
  passed the duplicate-admission/conflicting-input behavior test; 11.35 seconds
  including compilation. Total focused Rust verification: 170 passing tests.
- Both normal production entrypoints compile against the optimized library to
  `.local/replay-performance/round2/harness.exe` and `harness-worker.exe`.
  The harness `--help` smoke check passes. These isolated files do not replace
  the binaries currently loaded by the live application.

The bounded full-suite failures remain integration limitations; focused passing
checks and exact scientific comparisons are not a claim that either complete
repository suite passed.
