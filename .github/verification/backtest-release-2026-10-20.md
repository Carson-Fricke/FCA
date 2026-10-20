# Backtest release and July 30 MSFT comparison

Runtime source: `ba6db8b83518381ef27cbafa35a6b97af18a0007`.
Detailed receipts, durable jobs, timings and physical checksum verification are
retained under `.local/verification/backtest-release-2026-10-20/`.
This record supplements the implementation evidence in
`backtest-throughput-2026-10-20.md`; historical records remain unchanged.

## Scope and measurement

The committed `config/harness/smoke.yaml` and `momentum.rs` run MSFT's closed
July 30, 2026 regular session, 13:30–20:00 UTC, with identical strategy source,
parameters and input configuration except the caller-retained job ID. Both
environments already have the input data. No provider acquisition is timed or
claimed here. The dataset contains 2,063,951 trades and 948,959 quotes, for
3,012,910 replayed ticks. This quote strategy does not request bar subscriptions.

End-to-end durable duration is `updated_at - created_at` on the successful job:
admission through committed metrics and artifact inventory, including worker
dispatch/startup and strategy compilation. Local CLI wall time additionally
includes launcher startup and the smoke caller's two-second observation cadence.
These are bounded single-session observations, not statistical fleet estimates.

The local run uses the rebuilt optimized `harness-worker.exe`, the baseline's
isolated `local-data` store, fresh result/cache directories and a fresh JIT
workspace. A second intentionally allocated job reuses its compiled strategy.
The running user-facing local harness API was not part of these worker profiles.

## Local results

| Measurement | Before | After | Reduction |
| --- | ---: | ---: | ---: |
| Admission to durable results, fresh compilation | 82.743 s | 54.461 s | 34.2% |
| Complete smoke CLI wall time, fresh compilation | 84.171 s | 56.237 s | 33.2% |
| Work after compilation through durable results | 57.066 s | 22.398 s | 60.8% |
| Catalog planning / previous tick prescan | 10.033 s | 0.020 s | 99.8% |
| Replay, including its tick reading | 20.374 s | 16.755 s | 17.8% |
| Results, artifact generation and publication | 26.613 s | 5.561 s | 79.1% |

The new result phases are computing results 1.813 s, writing artifacts 3.185 s
and publication 0.544 s; small transitions account for the remaining time in
the combined result interval. Fresh compilation varied from 25.558 s before
to 31.215 s after and is included in the end-to-end figures.

The intentional cached-strategy run completed in 22.451 s from admission to
durable results, or 24.095 s of complete CLI wall time. Compilation/cache loading
took 0.420 s, replay 16.386 s, results 1.752 s, writing 3.105 s and publication
0.533 s. No old warm-run duration is inferred from a historical record whose
timing boundary differs.

Retained local IDs:

- `throughput-local-msft-20260730-ba6db8b`
- `throughput-local-msft-20260730-ba6db8b-warm`

## Correctness

Both local runs succeeded with 16 artifacts and exactly matching financial
metrics. All 16 published physical local files were verified against their
recorded byte lengths and SHA-256 checksums. Eleven trading artifacts are
byte-identical to the previous baseline, including fills, orders, actions,
position transitions and the complete equity file. The remaining files contain
job identity, reports or changed catalog provenance and are not expected to have
the same checksum across jobs.

Equity remains 3,012,913 full-resolution samples and 67,217,609 bytes, SHA-256
`53bb45338e808a4908cccde9806a1bc5b3ea29e83ef6a249835c89a671f9b6fe`.
There are two fills, one closed trade and net PnL of -11. All newly observed
terminal progress stages are marked completed, with final replay/result rows
and uploaded-byte counts populated.

Catalog planning now avoids quote/trade object reads. Tick object loading,
validation and replay still occur in the replay phase; the planning reduction
does not mean market data can be processed without reading it.

## Azure results

The deployment's own retained smoke job is `quant-smoke-39479397412-1`.
The primary comparison is the previous successful production deployment smoke,
`quant-smoke-39312485552-1`, which has the same accepted input except job ID.
This keeps the production storage and deployment-smoke execution path aligned.
The older isolated Azure proof took 137.089 s from admission to durable results
(137.804 s including observation); its different worker-startup/storage scope
is not used as the primary before/after percentage baseline here.

| Measurement | Previous deployment smoke | New deployment smoke | Reduction |
| --- | ---: | ---: | ---: |
| Admission to durable results | 118.957 s | 92.037 s | 22.6% |
| Admission to first compilation progress | 29.220 s | 34.493 s | -18.1% |
| Fresh compilation | 19.461 s | 20.062 s | -3.1% |
| Work after compilation through durable results | 70.276 s | 37.481 s | 46.7% |
| Catalog planning / previous tick prescan | 14.413 s | 0.652 s | 95.5% |
| Replay, including its tick reading | 23.580 s | 23.181 s | 1.7% |
| Results, artifact generation and publication | 26.882 s | 12.734 s | 52.6% |

New result phases: computing 2.124 s, writing 4.212 s, publication 5.857 s;
transitions account for the rest of the combined result interval. Both requested
datasets were already cached. Their parallel coverage preparation took about
0.418 s per dataset, versus about 3.916 s in the previous smoke.

The new job was admitted at `2026-10-20T19:38:41.781260463Z` and committed
success at `2026-10-20T19:40:13.817841535Z`. Worker dispatch/startup and fresh
compilation together consumed 54.556 s, about 59% of this single-session total.
Replay remains the largest active compute phase. This run does not demonstrate
a substantial Azure replay-kernel speedup: its measured improvement is primarily
catalog preflight, result materialization and coverage preparation. Local and
cloud results should not be presented as equal hardware/cache configurations.

All 16 Azure physical artifacts were downloaded and checked against their
published lengths and SHA-256 hashes. Financial metrics match the baseline and
local run exactly. The same eleven trading artifacts, including the full equity
file and every fill, are byte-identical across the old and new implementations
and both environments. The new Azure inventory totals 67,243,140 bytes; small
identity/provenance/report differences explain total-size differences between jobs.

The read-only observer captured 34 durable progress snapshots. Replay rows
advanced to 3,012,910; computing and writing advanced to 3,012,913; publication
bytes advanced through 4,811, 67,222,420 and further increments to 67,243,140.
Successful terminal stages are completed. Thus result generation and upload
produce visible progress before the atomic terminal inventory appears.

The execution worker `fca-ml-dev-backtest-2f8rp0y` completed with exit code 0
and zero container restarts. Its sibling finite worker completed, and the
small deployment-smoke control execution also completed successfully.

## Release verification

- [Source CI](https://github.com/Carson-Fricke/FCA/actions/runs/39479400609)
  passed. Linux harness workspace: 329 tests including documentation tests;
  lake workspace: 132 tests. Both formatting and Clippy checks passed.
- Python: 1,665 passed and one explicitly disabled live-Cosmos test skipped in
  71.13 seconds. Ruff lint/format, ty, wheel build and installed SDK smoke passed.
  This is Linux CI evidence; it does not supersede the separately documented
  over-budget Windows full-suite observation.
- UI checks, tests and production build passed. Unaffected Bicep validation was
  skipped by changed-product selection.
- The scoped implementation commit excludes unrelated local Thesis/workflow
  edits. No user execution was cancelled or automatically replayed, and no
  market catalog or provider data was reset for these measurements.
- [Main release and quant smoke](https://github.com/Carson-Fricke/FCA/actions/runs/39479397412)
  passed, including digest verification, authenticated ingress checks and the
  real July 30 backtest. API revision `fca-ml-dev-quant-api--0000088` is ready.
  API and execution use the same immutable runtime image:
  `fcau7enxjst4w72u.azurecr.io/fca-harness@sha256:b329464a1a1d620769a2e72bff58f075ee4f4d0cc5b56b71ea57dcd13effac59`.
  API resources remain 1 vCPU / 2 GiB, execution 4 vCPU / 8 GiB. Ingestion
  remains an ACA Job at 4 vCPU / 8 GiB, image digest
  `sha256:bbd266340f2a7adcecb42af08efc7f2739bc9d945c1c8771324b0ce08a62b502`.
- [Thesis and UI release](https://github.com/Carson-Fricke/FCA/actions/runs/39479397370)
  passed, including the coordinated backend deployment, Thesis runtime health
  smoke, UI checking/testing/build and Azure Static Web Apps deployment.
  After this composition deployment, the harness still runs the new shared
  digest on ready/latest revision `0000088`; its resource allocation is unchanged.
- The image publication job took 34m24s. Its uncached harness dependency cook
  took 23m13s and rebuilt bundled `libduckdb-sys`; the actual harness source
  build took 2m31s. The finite ingestion-image build reused cached layers.
  This release-build cost is separate from the execution durations above.
- Azure harness, local Thesis and local UI health checks returned HTTP 200.
  Serving and local SDK identity both equal
  `2e3878f58523ec8d36620cb4fde001408995fda0916fc63042b0de576aa4e4bb`.
  The local Thesis configuration still selects the entire remote Azure harness.
  Its protected harness-status route correctly rejected an unauthenticated
  verification request with 401; no authenticated browser-session assertion is
  inferred from that request.
