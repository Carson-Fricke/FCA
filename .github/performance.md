# Performance and Memory Budgets

Harness task H8.2. This document explains the benchmark suite in
`tests/performance/`, publishes the captured throughput, latency, artifact
write, and memory baselines, and defines the regression rule and report.

The benchmarks are opt-in: they never run in the default test path or CI,
and they introduce no wall-clock measurement, unseeded randomness, or
network access inside the synchronous kernel. All measurement code uses the
standard library only (`time.perf_counter_ns`, `statistics`, `tracemalloc`);
no benchmark dependencies were added.

## Job profiles and universe-width tiers

Every workload is deterministic and synthetic: seeded event patterns with
exact decimals, no network, no wall-clock input, no unseeded randomness.

| Benchmark | Engine path | Metric | Workload |
| --- | --- | --- | --- |
| `test_replay_throughput.py` | `harness.data.replay.LakeReplayFeed` merged replay (heap merge + fingerprints) | events/s | 20,000 mixed quote/trade events per tier |
| `test_matching_cost.py` | `harness.fills.SimpleTouch` / `harness.fills.TradeThrough` decisions | us/order | 20,000 alternating buy/sell orders |
| `test_risk_latency.py` | `harness.risk.pipeline.RiskPipeline.evaluate` (4-rule allow path and reduce-restart path) | us/decision | 10,000 decisions per path |
| `test_recorder_artifacts.py` | `harness.results.recorder.RunRecorder` streaming writes + manifest close | rows/s | 20,000 rows (5,000 x action/order/fill/marker) |
| `test_memory_budget.py` | Representative streaming profile: feed -> `EventBus` -> recorder with payloads | peak MiB, bytes/event | 10,000 events per tier |

Universe-width tiers cover the supported replay shapes:

| Tier | Symbols |
| --- | --- |
| `narrow` | 1 |
| `medium` | 8 |
| `wide` | 32 |

The streaming profile keeps memory bounded: peak is flat across tiers
because the replay holds one lookahead event per source and the recorder
flushes bounded batches, so the wide-universe run costs the same memory as
the narrow one.

## How to run

Benchmarks are skipped unless the `FCA_PERF` environment variable is `1`
(enforced in `tests/performance/conftest.py`; the `performance` marker is
registered there, so `--strict-markers` stays satisfied without touching
`pyproject.toml`).

```powershell
# Full benchmark suite plus the regression report
$env:FCA_PERF = '1'
uv run --python 3.12 --extra dev pytest tests/performance -q

# One area only
uv run --python 3.12 --extra dev pytest tests/performance/test_risk_latency.py -q

# Emit the regression report without failing (report-only mode)
$env:FCA_PERF_REPORT_ONLY = '1'
uv run --python 3.12 --extra dev pytest tests/performance/test_regression_report.py -q
```

A full gated run takes roughly 25 seconds. Without `FCA_PERF=1` the
directory is skipped in a fraction of a second, so normal `pytest` and CI
runs are unaffected.

## Methodology

- Timing is the median of 5 measured runs after 1 warmup run
  (`time.perf_counter_ns`). Workload sizes keep each run above tens of
  milliseconds so per-run noise stays far below the regression budget.
- Memory is the minimum of 3 `tracemalloc` peak readings over the
  streaming profile, reported both as absolute peak and per event.
- Benchmarks record values instead of asserting absolute times, so the
  suite is tolerant of machine speed. Sanity checks (all decisions fill,
  risk actions match the configured path) run outside the timed region.
- The regression report reuses in-session results when the whole directory
  runs, and reruns the benchmarks itself when invoked alone.

## Captured baselines

Captured 2026-09-04 with Python 3.12.14 on Windows 11 (AMD64), from the
canonical commands above. Baselines are machine-class specific: recapture
on your machine before judging regressions.

| Metric | narrow (1 symbol) | medium (8 symbols) | wide (32 symbols) |
| --- | ---: | ---: | ---: |
| `replay.throughput.<tier>` | 164,231 events/s | 144,946 events/s | 136,847 events/s |
| `memory.streaming.peak_mib.<tier>` | 2.456 MiB | 2.455 MiB | 2.466 MiB |
| `memory.streaming.peak_bytes_per_event.<tier>` | 257.6 B/event | 257.5 B/event | 259.0 B/event |

| Metric | Baseline |
| --- | ---: |
| `matching.simple_touch.cost_per_order` | 3.35 us/order |
| `matching.trade_through.cost_per_order` | 3.52 us/order |
| `risk.pipeline.allow.latency_per_decision` | 41.49 us/decision |
| `risk.pipeline.reduce.latency_per_decision` | 35.75 us/decision |
| `recorder.artifacts.rows_per_second` | 102,404 rows/s |

The reduce path is cheaper than the four-rule allow path because it runs a
single configured rule that reduces once and rechecks once.

## Regression rule

A benchmark regresses when a fresh run is more than 20 percent worse than
the captured baseline **in the unfavorable direction**:

- Throughput metrics (`events/s`, `rows/s`, higher is better): fresh
  `< 0.80 x baseline`.
- Cost, latency, and memory metrics (`us/*`, `MiB`, `bytes/event`, lower is
  better): fresh `> 1.20 x baseline`.

`tests/performance/test_regression_report.py` compares the captured
`tests/performance/baselines.json` with a fresh run and emits a focused
per-metric report, failing on any regression:

```text
performance regression report
baseline document: ...\tests\performance\baselines.json
regression budget: 20% in the unfavorable direction
[ok] replay.throughput.narrow: baseline 164,231.12 events/s, fresh 162,185.22 events/s, delta -1.2% (budget 20%)
[REGRESSION] recorder.artifacts.rows_per_second: baseline 102,404.30 rows/s, fresh 70,000.00 rows/s, delta -31.6% (budget 20%)
```

CI usage: run the gated suite in a scheduled or manual workflow with
`FCA_PERF=1`; a failure output is the focused report above. Use
`FCA_PERF_REPORT_ONLY=1` to emit the same report as a warning without
failing the job.

## Updating baselines

Recapture after an intentional engine-path change, on the same
machine class, and commit the refreshed file:

```powershell
$env:FCA_PERF = '1'
$env:FCA_PERF_UPDATE_BASELINES = '1'
uv run --python 3.12 --extra dev pytest tests/performance -q
Remove-Item Env:FCA_PERF, Env:FCA_PERF_UPDATE_BASELINES
```

The update run rewrites `tests/performance/baselines.json` (schema,
environment, workload sizes, and per-metric values/directions) from the
fresh run. Never update baselines to absorb an unexplained regression;
investigate the report first.
