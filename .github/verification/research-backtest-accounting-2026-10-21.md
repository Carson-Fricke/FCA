# Backtest accounting, partial fills and research catalogs — 2026-10-21

## Scope and execution environment

This verification covers reduced accounting/output overhead, cumulative partial
fills, and the Backtests/Strategies catalogs and detail views. Historical jobs
and their frozen sources are unchanged.

Four complete MSFT January–July 2026 executions use the original accepted windows
(`2026-01-01T00:00:00Z` through `2026-08-01T00:00:00Z`, end exclusive), subscriptions,
data and parameters. Each consumes 170,466,873 input events across 145 sessions.
Two optimized Windows native workers use the actual Azure catalog, market and
results adapters in private container `harness-research-verify-b479125eb056`.
They share that isolated queue, including waiting behind earlier executions.
This does not deploy a new image to the shared Azure research service.

Retained engineering records and artifacts are under
`.local/research-records-20261021/`. `benchmark-context.json` records identities,
accepted configuration hashes, worker hash and topology. `benchmark-report.json`
retains authoritative records through publication; `results-analysis.json`
compares immutable outcomes and reports queue, execution and phase timings.

## Accounting/output parity

The first two runs retain the original strategy sources, including the original
continuation defect, to isolate engine equivalence from the strategy correction.

| Case | Retained verification job | Old equity bytes | New equity bytes | Logical / retained samples |
| --- | --- | ---: | ---: | ---: |
| No-trade reversion | `research-verify-bb8bb82484964c0d92e3842a3c5e64ae` | 3,831,181,263 | 3,583 | 170,414,239 / 2 |
| Active continuation | `research-verify-d3fe4e51e7534b35bed4d5ab1a69b9bf` | 4,478,560,053 | 6,715,166 | 170,414,426 / 129,584 |

Both reproduce the original dispatch hash and complete fill sequence exactly
(zero and 187 fills). Every existing value in the `counts`, `equity`, `trades`
and `execution` metric blocks matches exactly. New metadata declares logical
sample count, reporting count/cadence and causal sample basis separately.

| Phase totals, seconds | Historical no-trade | New no-trade | Historical active | New active |
| --- | ---: | ---: | ---: | ---: |
| Queue | 29.286 | 272.454 | 704.478 | 271.096 |
| Replay | 1,029.104 | 1,404.213 | 971.787 | 1,443.484 |
| Compute + write + publish | 310.117 | 9.533 | 667.539 | 355.550 |
| Complete admission through publication | 1,395.768 | 1,772.838 | 2,368.966 | 2,156.630 |

Historical workers ran on Azure Container Apps; the new runs use Windows native
workers with different contention and network locality. These are complete
measured durations, not a controlled host-to-host speed comparison. End-to-end
latency remains dominated by replay and queueing. No overall speedup is inferred.
The initial verification recipe left the first pair queued while correcting an
explicitly rejected, never-admitted third configuration; that waiting is included.
The rejected identity and reason remain in the context record, without replay.

## Partial fills

The corrected implementation is `config/harness/programs/msft_continuation.rs`.
It uses `FillLedger` for every actual fill, tracks requested quantity until fully
filled, calculates quantity-weighted entry price, submits exits for actual signed
inventory, and closes an episode only after full exit and zero inventory.
Actual fee, position and pending-order metrics are exposed. Pending cancellation
or rejection is not inferred from elapsed time or an absence of fills.

The SDK behavior check reproduces the original 121-share partial entry as
4 + 45 + 72 shares and exits 4 + 117 shares. It verifies remaining inventory,
completion, weighted price, deduplication, fees and marked P&L. The momentum
example also uses cumulative order accounting.

Complete corrected-source executions:

- `research-verify-201ee4abd87e4ec9af2ebb93ff5c948c`: original continuation parameters.
- `research-verify-8ec2dadb2e5b4778a63f27b370fe3eed`: original parameter neighbor.

Both succeeded through publication over the entire window. Actual fills reconcile
exactly to final cash, marked net P&L, execution fees and custom inventory/fee
metrics. All fill-day closing quantities are zero, as are pending orders and
overnight violations. The configured fee schedule charges zero; nonzero actual
fees are additionally verified by the SDK accounting behavior tests.

| Corrected case | Fills / episodes | Final quantity | Marked net P&L | Queue / execution / end-to-end seconds | Compute + write + publish seconds | Equity bytes |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| Original parameters | 263 / 27 | 0 | 842.9157 | 1,513.324 / 1,798.811 / 3,312.134 | 23.174 | 375,424 |
| Parameter neighbor | 148 / 13 | 0 | -864.0288 | 1,897.021 / 1,430.496 / 3,327.518 | 17.301 | 178,747 |

The original-parameter entry still sells 121 shares as 4 + 45 + 72 partial fills.
Its corrected exit buys the full 121 as 1 + 32 + 81 + 7, instead of treating the
first one-share exit fill as a completed position. The earlier frozen case ended
with +532 shares and misleading marked P&L of 31,001.9835. The old neighbor ended
with -200 shares. These engineering corrections do not count as additional
strategy-selection evidence, and their source is not substituted into old jobs.

## Catalogs and inspection

The live local UI against the configured Azure harness lists all six original
`t5-ws` jobs (three succeeded, three failed) and both authored strategy programs.
Catalog membership comes from retained submissions, neutral tool-job references
and existing report projections. Failed/status-unavailable executions remain
visible; successful report import is not a discovery prerequisite.

Strategy discovery shares the artifact catalog's authored-program/published-bundle
projection. Detail reads real source/manifest/history and links executions to
their own frozen source/parameters. Catalog projection detaches cached objects
before decorating display identities.

Inspection exposes actual fills, final portfolio, custom metrics, phase timing,
accepted inputs and artifact metadata. HTTP checks cover workspace isolation,
pagination, strategy filtering, no-report discovery, unknown identity rejection,
and preserving the last observation on read failure. Report previews refuse
oversized Parquet before fetching it; published financial statistics/trades remain
available. Chart cadence never changes the full-stream metric definitions.
Program associations require retained attribution or an explicit submitted
strategy ID; an execution-local instance name cannot invent a strategy link.
The detail summary now reads the published canonical metric names, including a
per-sample Sharpe with enough significant digits to preserve its small magnitude.
Unavailable equity renders an explicit preview state. Closed trade slices and
actual fills/positions remain inspectable separately from the overview.

The shared job observer has no two-minute cutoff. A rendered-card test observes
for 125 simulated seconds, recovers a failed status read, updates a queued receipt
to a terminal failure and stops reading after navigation. No read submits/replays.

## Integration verification

- `uv lock --check`: passed.
- Repository `ruff check .`, `ruff format --check .`, `ty check`: passed.
- Full Python suite: 1,689 passed, one optional live-Cosmos test skipped, first in
  328.51 seconds under concurrent research/build load and finally in 221.06 seconds.
  The three-minute budget was exceeded in both runs; this is not recorded as budget
  compliance. Required delivery architecture and job interruption/acknowledgement/
  admission suites passed. The later focused catalog checks also passed after
  strengthening explicit program attribution and instance-alias coverage.
- UI: the complete suite passed 287 tests, with one skipped; the additional
  canonical summary/absent-equity behavior check passed in the focused four-test
  statistics suite. The updated execution-card suite passed all ten checks.
  Svelte check and production build passed; two existing CSS compatibility
  warnings remain in `SketchToolCard.svelte`.
- Rust formatting and workspace Clippy passed after removing a redundant clone of
  a Copy metric value. The first test build could not link `msvcrt.lib` outside the
  installed MSVC environment. A subsequent no-run build rebuilt bundled DuckDB
  and reached its 1,200-second bound. All still-unexecuted suites then completed
  with bounded package/target isolation using `vcvars64.bat`, covering all twelve
  workspace packages and doc tests. Required API job identity, duplicate admission
  and ownership cases passed. The CLI check caught stale example source digests;
  both active YAML configs, the canonical JSON fixture and configuration hash
  golden were updated together and the failing checks then passed. Individual
  command results remain in `rust-targets.json`, `rust-remaining.json` and
  `rust-tail.json`; earlier failures are retained rather than overwritten.

Live API acceptance results are saved in `ui-api-acceptance.json`; browser proof
is `backtests.png` and `strategy-executions.png` in the same local evidence folder.

Deployment and the existing workspace program's new revision require the current
worker/SDK to be rolled out together. This verification changes local source and
the local serving API; it does not claim that the shared Azure image was updated.
