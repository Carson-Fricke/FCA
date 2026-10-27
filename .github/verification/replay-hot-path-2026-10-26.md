# Replay hot-path optimization — 2026-10-26

## Scope and invariants

Implemented the four requested changes in the current Rust replay:

1. `EventProjection` prepares numeric scales once per run and consumes owned
   event identifiers instead of cloning them during every conversion.
2. The venue retains historical orders but matches only stable, symbol-scoped
   active indexes. Its last-market state contains prices and liquidity rather
   than copies of feed identifiers.
3. Dispatch hashing reuses its identity buffer and calendar prefix, preserving
   the exact existing UTF-8 framing and SHA-256 bytes.
4. Arrow decoding binds column types once per batch, borrows string cells until
   ownership is needed, and maps directly into research replay events. The
   intermediate normalized-event vector and subsequent identifier clones are
   removed. Nulls, accepted field names, strict numeric types, row identities,
   nanosecond bounds, chronological merging and immutable source pins remain.

No execution fidelity, pricing, fee, signal, forward-window, job identity or
persisted format was changed. Historical research submissions are retained.

## Measurement method

The workstation is an AMD Ryzen 9 5900HX. Optimized builds use the existing
release profile. Original binaries were retained before source changes.
Benchmark helpers and raw outputs are in ignored `.local/replay-performance/`;
the temporary microbenchmark source is `.local/hot_path_bench.rs`.

The benchmark runner uses the production `NativeBacktestSubmitter`, durable
admission and finite worker composition with an isolated result root and a
retained fresh `job_id` for each execution. It reuses the already-cached local
market lake. It does not restart or replace the running research stack.

Both one-session comparisons use MSFT on 2026-01-02, with the original committed
continuation/DC and reclaim programs and parameter assignments. Full-window
reruns use their original Jan–Jul 2026 development configuration and embedded
source. The user explicitly requested rerunning these backtests; forward data
remains sealed. Full-window baseline stage times come from the retained original
jobs and are observational comparisons, not a controlled replay on an idle host.
The live local harness can continue competing for machine resources.

Compare replay-stage times separately from compilation, queueing, data setup,
result computation and publication. New execution identities and engine code
fingerprints are expected to differ; compare scientific results independently
of those metadata fields.

## Focused behavior checks

Passing development checks:

- `cargo test --manifest-path src/harness/Cargo.toml -p backtest -p marketdata --no-default-features`
- `cargo test --manifest-path src/harness/Cargo.toml -p backtest --test dispatch_identity --test risk_and_stops -p marketdata --test arrow_replay`
- `cargo test --manifest-path src/harness/Cargo.toml -p backtest --test risk_and_stops -p harness-research --test lake_parallel --test study_frames`

The new strict Arrow tests cover exact decimals, aliases, row identities across
batches, null optional columns and rejected numeric types. The dispatch golden
covers negative timestamps, nanosecond truncation, second boundaries, Unicode
identifiers and `u64::MAX` sequences. Replay behavior verifies that terminal
orders do not fill again. Existing frame, sticky-stop, risk, nanosecond, coverage,
streaming, valuation and exact reporting tests remain required.

The first one-session fill-probe run reached an existing authored-program failure
(`metric value must be finite`). Its retained job is
`job-perf-53e59aad42db4048abc12a80ef24fc2b`; it is excluded from successful-run
comparisons. Its initial local-composition failure occurred before execution and
was reconciled using the same retained ID after setting the configured local
ingestion-worker executable.

Windows denied replacing the running `harness.exe` and `harness-worker.exe`.
The isolated runner instead links the successfully built optimized library:
`cargo build --release --manifest-path src/harness/Cargo.toml -p api --lib`.
The live application still uses its original loaded binaries. A normal worker entrypoint
is also built as `.local/replay-performance/harness-worker-optimized.exe`,
ready for a later restart after the existing research executions are reconciled.
The normal API and worker entrypoints also compile to
`.local/replay-performance/optimized/harness.exe` and `harness-worker.exe`;
the optimized API binary passes its `--help` smoke check.

## Results

The full continuation/DC rerun succeeded with identical scientific outcome,
headline metrics and dispatch hash after normalizing only execution identity and
excluding the expected configuration/code fingerprints. It consumed 170,466,873
market events and produced the same 612 orders and 612 fills.

| Full continuation/DC stage | Original retained run | Optimized rerun |
| --- | ---: | ---: |
| Replay | 1,134.341 s | 293.244 s |
| Result computation | 100.763 s | 84.306 s |
| Worker execution through publication | 1,237.575 s | 379.482 s |

Original job: `job-d1484a589d454f33b947b1e328fc5a8c`.
Optimized job: `job-perf-64991368b03647d5acd4368f15e92b80`.
Dispatch hash: `a3085750bf691fbf0f6aa870b44ffd7d67444c3f7b22c7c8b223d5cd304af149`.
The result-computation stage remains substantial; a one-minute end-to-end target
has not been reached.

Controlled one-session replay times (985,494 raw events; no fills because the
original daily-context warm-up remains intact):

| Program | Before replay | After replay | Before total wall | After total wall |
| --- | ---: | ---: | ---: | ---: |
| Continuation/DC | 4.901 s | 0.990 s | 8.060 s | 2.326 s |
| Reclaim | 4.282 s | 1.058 s | 5.468 s | 2.367 s |

Both session outcomes, headline metrics and dispatch hashes are identical under
the same execution-metadata normalization. Compilation/cache differences are
visible in total wall time, so replay is the cleaner comparison.

The full reclaim rerun also succeeded with identical scientific outcome,
headline metrics and dispatch hash. It consumed the same 170,466,873 raw events
and produced the same 168 orders and fills.

| Full reclaim stage | Original retained run | Optimized rerun |
| --- | ---: | ---: |
| Replay | 1,022.410 s | 266.011 s |
| Result computation | 23.442 s | 19.390 s |
| Worker execution through publication | 1,048.300 s | 287.120 s |

Original job: `job-04a9104f6ef34ef8b142c9c30c07a0da`.
Optimized job: `job-perf-6830bd02ee0648e19fd6608372c78858`.
Dispatch hash: `4abd71372222143f77ef49f0adce3baae5d7be37e1bf2c176981eabdeaa140a7`.

Replay speedups are 3.87x (continuation/DC) and 3.84x (reclaim).
Complete worker execution through publication improved by 3.26x and 3.65x.
The optimized caller wall times, including admission and startup, were 379.760 s
and 287.536 s.

Raw comparisons are retained as `dc-comparison.json`, `reclaim-comparison.json`,
`dc-session-comparison.json` and `reclaim-session-comparison.json` under
`.local/replay-performance/`. The comparator normalizes the explicit execution
ID and excludes only manifest configuration/code fingerprints; all remaining
outcome fields and headline metrics compare exactly.

## Repeated release microbenchmarks

Three paired before/after runs were measured after verification completed,
without running a full benchmark or compiler simultaneously. These are isolated
component costs, not additive estimates of complete job time. Medians:

| Component | Before ns/event | After ns/event | Speedup |
| --- | ---: | ---: | ---: |
| Venue, no historical orders | 182.8 | 16.0 | 11.43x |
| Venue, 100 filled historical orders | 274.3 | 18.2 | 15.07x |
| Venue, 300 filled historical orders | 541.4 | 19.4 | 27.91x |
| Venue, 600 filled historical orders | 893.1 | 18.7 | 47.76x |
| Enqueue, dispatch, canonical identity and hashing | 770.0 | 285.6 | 2.70x |
| Resident synthetic Arrow trade decoding, no I/O | 773.9 | 369.8 | 2.09x |
| Fixture-to-engine trade conversion | 2498.8 | 204.2 | 12.24x |

The conversion benchmark supplies an owned clone to the new consuming API to
retain the input for the next iteration; production transfers ownership directly.
The unchanged quote-midpoint control measured 221.9 versus 229.1 ns/event.
The synthetic half-spread arithmetic helper remains measurable in the diagnostic
but is no longer called per event in production. Raw samples and medians are
retained in `.local/replay-performance/micro-summary.json`.

## Integration verification

- `uv lock --check`: passed.
- `uv run --extra dev ruff check .`: passed.
- `uv run --extra dev ruff format --check .`: passed after fixing an encoding
  error in the newly added README entries; 826 files already formatted.
- `uv run --extra dev ty check`: passed.
- `git diff --check`: passed.
- The complete Python suite exceeded the 180-second bound without an assertion
  failure observed before termination. The unchanged suite was not rerun.
- The full Rust workspace attempt exceeded the 180-second bound during
  compilation. The unchanged workspace suite was not rerun.
- A hard-bounded isolated Python run of
  `tests/thesis/http/test_application_parity.py` and
  `tests/thesis_runtime/test_delivery_architecture.py` passed: **82 tests**,
  15.93 seconds of pytest time (21.62 seconds including process setup).
- `cargo test --manifest-path src/harness/Cargo.toml -p backtest -p marketdata -p harness-research`
  passed: **131 tests**, 60.47 seconds including process setup and doc tests.

The broad timeout results do not certify the complete repository. Logs are
retained under `.local/replay-performance/integration-*.log`; isolated check
commands, durations and results are retained in `python-isolation.json` and
`rust-owned.json`. No unrelated Python/runtime implementation or expensive test
was changed to obtain a passing result.
