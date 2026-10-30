# Backtest latency diagnosis - 2026-10-27

## Finding

The cited conversation contains jobs executed before the optimized harness was
restarted. The current binaries reproduce a separate, substantial SDK overhead
for strategies subscribed to quotes: routing validation rebuilds and allocates
the consumer/dependency graph repeatedly for each callback. The earlier large
speedups for bar-only continuation/reclaim programs do not measure that path.

No production source was changed in this diagnosis. Existing pending Thesis
changes were retained. Diagnostic programs and results are isolated under
`.local/backtest-diagnosis-2026-10-27/`; no running application was restarted,
no existing execution was resubmitted, and no provider ingestion was required.

## Historical evidence and deployment timing

Read the linked chat and its scoped timeline through the local Thesis HTTP API,
then inspected the 109 retained harness job records. The source snapshot is
commit `a39e6e7`; the harness and worker binaries were built October 26 at
17:41:25 America/Los_Angeles, or October 27 at 00:41:25 UTC.

For fade execution `job-8f80bcabfdc34e6a82d4d8380ab5da0c`:

- Accepted October 26 at 18:43:25.383400 UTC.
- First compiling progress October 26 at 20:30:17.850611 UTC: **6,412.467 s
  queued**, or 106.87 minutes.
- Compilation: 4.236 s; reading-data preparation: 1.192 s.
- Replay: **1,984.291 s**, or 33.07 minutes, for 170,466,873 raw events.
- Results: 26.932 s; artifact publication: 0.509 s.
- Finished October 26 at 21:03:55.542945 UTC, several hours before the optimized
  binaries were deployed. The other captured slow examples also predate this
  restart; the initial snapshot contained no queued or running jobs.

Historical execution intervals overlap at two workers. The source defaults to
two workers and ignored `.env` does not override `HARNESS_EXECUTION_MAX_WORKERS`.
The conversation's assertion that the runner was stalled and single-lane is
therefore not established by the execution evidence. Long replay jobs were
progressing while later jobs waited behind them.

The chat contains **4,576 `get_job_status` tool-result messages** across 18
conversations, including 3,791 from one child. The observed peak is 108 such
results in a minute. This is unnecessary model/session work; its contribution to
native replay latency was not measured and is not assigned a causal percentage.

Eight retained jobs report Windows `failed to persist temporary file: Access is
denied. (os error 5)`. These are genuine execution/persistence failures, separate
from slow replay. They did not recur in the six isolated runs. Their OS-level
cause remains unconfirmed; this diagnosis does not attribute them to antivirus,
polling, or a particular file-sharing race without a trace.

## Fresh reproduction on the deployed executables

Started an isolated API on port 18001 using the exact deployed `harness.exe` and
`harness-worker.exe`, a separate results root and one execution slot. The existing
local market-data catalog/cache was reused. Original accepted programs and
parameters were copied from the continuation and fade records; only execution
identity and replay dates changed. Each new ID and immutable input reference was
saved before POST admission, and the returned ID was checked. Outcomes were
observed to terminal state before submitting another run.

All six executions use the one closed session MSFT 2026-01-02: 696,291 trades,
288,813 quotes and 390 source bars, totaling **985,494 raw events**. Cached
coverage reports one session for each source type. No forward-window data was
used. Each program ran three times sequentially.

| Program | Replay samples, seconds | Median replay |
| --- | --- | ---: |
| Continuation, bars only | 0.595035, 0.733396, 0.692255 | **0.692255 s** |
| Fade, bars and quotes | 3.722178, 3.834838, 3.815842 | **3.815842 s** |

Both programs consume the same raw events because execution matching still
requires trades and quotes. Only fade requests quote callbacks into authored
code. It remains **5.51x slower** on this session despite the engine arithmetic
optimizations being present in both runs.

The first continuation compilation took 33.941964 s; the first fade compilation
took 4.188809 s. Subsequent compilation-stage samples ranged from 0.371993 to
0.447060 s. Warm caller wall times were about 2.02 s for continuation and 5.04 s
for fade, including the observer's one-second status interval. Replay medians
come from durable stage timestamps, not that observer interval.

All six executions succeeded. The published `backtest.json` hashes were checked
against their artifact entries. Metrics and scientific outcome fields excluding
the manifest match within each program after normalizing job identity. All six
retain the same dispatch hash:
`4c36f3eecc92ebed3db83d5d599ebbc8682b4e6764ab63490ff25996b9315f7f`.

These one-session runs produce **zero orders and fills** because the original
programs retain their warm-up requirements. They isolate ingestion-independent
replay/callback cost, not active-position/fill behavior. No new full January-July
wall-time claim or full-window performance projection is made.

Raw inputs, retained job IDs, acknowledgements, records and stage timings are in
`*-original.json`, `*.submission.json`, `*.receipt.json`, `*.record.json` and
`reproduction-summary.json`. `verify-evidence.py` checks the evidence and writes
`verified-evidence.json`.

## SDK callback isolation

Compiled an optimized standalone Rust probe against the current release SDK
library. It contains the original fade source and calls its actual exported SDK
quote trampoline. Controls use its direct quote method, an empty quote callback
with the same subscriptions/wavelet dependency, and an empty callback with the
same own subscriptions but no indicator dependency. No production SDK was patched.

Each timing has five samples of 100,000 callbacks after warm-up. Allocation
counts cover a separate 100-callback interval, excluding program construction;
timings disable allocation counting but retain the allocator's flag branch.
Thus these are controlled microbenchmarks, not sampled CPU profile percentages.

| Path | Median per quote | Heap allocations per quote | Requested allocation bytes per quote |
| --- | ---: | ---: | ---: |
| Original strategy method directly | 0.244526 us | 2 | 16 |
| Original strategy through SDK trampoline | 9.746506 us | 230 | 22,682 |
| Empty method, same wavelet dependency | 9.456195 us | 228 | 22,666 |
| Empty method, no indicator dependency | 5.065172 us | 126 | 11,410 |

An empty callback retains almost all the cost. The wavelet dependency nearly
doubles routing cost even though it subscribes to daily bars and does not
calculate a wavelet transform for these quote inputs. This points to SDK
subscription/topology reconstruction, rather than the numerical indicator or
strategy decision, as the main remaining cost in the quote callback path.

The source mechanism is:

- `strategy-sdk/src/export.rs::borrow_program` validates before routing;
  `finish_callback` validates again afterward.
- `strategy-sdk/src/consumer.rs::validate_routing` calls `routing_signature`,
  which recursively collects owned subscription vectors, canonicalizes them,
  allocates dependency signatures and uses a fresh visited set.
- `consumer.rs::route` validates before visiting dependencies, after visiting
  them and after calling the consumer. It also visits a daily-bar dependency on
  every quote, allocating its dependency list and traversal set.

Raw source and probe output: `fade.rs`, `sdk-probe-tail.rs`, `sdk-probe.rs`,
`sdk-probe.exe` and `sdk-probe.txt` in the diagnostic directory.

## Recommended implementation order

1. Give the SDK a reusable dispatch plan and allocation-free checks of stable
   subscriptions/topology. Eliminate repeated signature reconstruction, while
   preserving mutation rejection, dependency order, shared-indicator single
   delivery, causal availability and sequence retention. Simply disabling the
   existing checks would change behavior and is not the recommended fix.
2. Let bar strategies read a host-maintained latest-quote/spread snapshot at the
   bar decision. That avoids tens of millions of authored quote callbacks while
   keeping every trade/quote available to the matching engine. It requires an
   explicit causal SDK contract; dropping execution events is not equivalent.
3. Address queue feedback and obsolete-work cancellation as caller/worker
   concerns. Use bounded concurrency and measured capacity; increasing worker
   count alone does not remove the per-quote CPU cost. Separately trace and fix
   Windows atomic-publication failures, and stop status-tool polling storms.

## Verification and limits

- Six fresh production-boundary runs succeeded in isolated results storage.
- The evidence comparator passed, including artifact integrity and repeat output
  equality. Five samples per SDK probe control were retained.
- No production implementation or automated tests changed, so broad suites were
  not rerun over the unrelated pending Thesis work.
- The diagnostic API exited after the runs. The original local stack remains up.
- Prior full-suite timeout limitations remain as recorded in the October 26
  verification; this diagnosis does not reclassify them as passing.
