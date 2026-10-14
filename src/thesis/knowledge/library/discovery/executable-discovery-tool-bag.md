---
id: executable-discovery-tool-bag
title: Executable discovery tool bag and composition recipes
version: 4
created: 2026-10-09
retrieved: 2026-10-13
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: a904522b49994951f27c89f17e1f04e8ee5444d8639ce857c2885d16e4b0ce79
tags:
  - discovery
  - indicators
  - wavelets
  - directional-change
  - composition
  - sdk
  - strategy-sketch
collections:
  - discovery
  - model-cards
links:
  - from-market-idea-to-trading-construction
  - investigating-representations-and-episodes
  - developing-a-candidate-through-evidence
  - directional-change-events
  - perceptually-important-points
  - discovery-study-design
  - historical-timing-and-leakage
  - publishing-research-exhibits
relationships:
  - type: relates
    target: directional-change-events
  - type: relates
    target: perceptually-important-points
trust:
  tier: curated
  state: published
---
# Executable discovery tool bag

Start from the proposed market mechanism and develop a strategy sketch. Choose
a representation that makes the mechanism observable, then explore conditioning,
entry, exit, and abstention. These are building blocks, not suggested alpha or a
mandatory checklist. Existing studies can challenge the sketch; their questions
need not define it. Retrieve method cards and the current SDK before authoring.

For the research process, start with
[From market idea to trading construction](from-market-idea-to-trading-construction),
[Investigating representations and episodes](investigating-representations-and-episodes)
and the [worked research loop](developing-a-candidate-through-evidence). Choose a
kernel to resolve a design question; collecting more feature statistics is not
itself development of a trading construction.

The authoritative executable signatures live in
`harness-strategy-sdk/src/building_blocks.rs` and `outputs.rs`, available through
`read_sdk_reference`. Programs compose these kernels inside `MarketConsumer`
callbacks and may package them as reusable authored indicators. Each symbol and
timeframe needs independent state. Bar windows become usable at their declared
availability, not their opening timestamp. Trade and quote inputs are event
streams; derived output windows have a declared fidelity.

| Executable component | Inputs and output | Useful for | Limitations |
| --- | --- | --- | --- |
| `RollingMoments` | Trailing values → mean and population dispersion after warm-up | Compression, normalization, state conditioning | Window choice; not robust to spikes; dispersion is not an edge |
| `Ema` | Values → exponentially weighted value | Smooth context and fast/slow structure | First-observation initialization; choose/report warm-up; lag |
| `Rsi` | Prices → rolling simple-average gain/loss ratio | Asymmetric move conditioning | Explicitly not Wilder RSI; flat windows undefined |
| `Atr` | Valid high/low/close → simple mean true range | Price-scale thresholds and range context | First range lacks prior close; gap behavior depends on input continuity |
| `Vwap` | Chosen price and volume → rolling weighted mean | Location relative to traded activity | Bar-close/typical-price proxy differs from trade VWAP; zero-volume undefined |
| `CrossoverAge` | Fast/slow and observation time → direction, age, crossing flag | Duration/hazard questions and state persistence | Equality retains prior state; crossing alone is not a profitable trigger |
| `DirectionalChange` | Price and availability time → confirmed events | Event clocks, reversal/continuation setups | Absolute threshold; confirmation lag; earlier extreme is retrospective |
| `perceptually_important_points` | Trailing path and point count → selected indices | Path-shape compression and invented shape features | Vertical-distance variant; whole shape available only at window end |
| `WaveletIndicator` | Completed bars → immutable as-of coarse energy, signed trend and scale energies | Multi-timeframe structure and regime conditioning | Managed trailing Haar; explicit warm-up, missing and availability; no centered padding |
| `FittedNormalization` | Development observations → frozen mean/scale; later values → normalized value | Explicit training/inference separation | Constant training windows invalid; regime drift; no automatic refitting |

Full MODWT, Morlet CWT, and Hilbert kernels exist inside the backtest package,
but are not this SDK's callable authoring surface. Autoencoders, PCA, and other
learned representations require an explicitly authored implementation and
supported dependencies; a knowledge card alone does not make them executable.
Do not claim their execution without a successful validated program/job.

## Compose rather than stop at a statistical question

For a compression/release idea: use a slower EMA or ATR for context, trailing
Haar detail energy or range dispersion for compression, and confirmed DC events
for release. Compare representations and inspect event sequences before deciding
whether any directional opportunity is plausible. The sketch specifies when to
abstain, how the release becomes observable, entry delay, exit logic, and which
assumptions remain unsupported. Trials then challenge those assumptions.

Other directions include elapsed time since a crossover with a duration-based
entry condition; trailing PIP slopes with spread-based abstention; and frozen
normalization of order-flow features to identify unusual activity. Each is a
possible composition, not a prescribed strategy or a claim of profitability.

## Managed wavelet consumption

Read read_sdk_reference(topic="wavelet") for the current native API. WaveletIndicator
owns trailing state and subscribes to exact completed bars through MarketConsumer.
The transform kernel is internal; authored programs must not reimplement wavelets.

This usage fragment belongs inside a study or strategy; read the full template for
its remaining callbacks and publication contracts:

```rust
use harness_strategy_sdk::{Indicator, WaveletIndicator};

// In construction:
let wavelet = WaveletIndicator::new(vec!["MSFT".into()], "1d".into(), 32, 8)?;
// Store wavelet in the consumer. Its indicators() returns vec![&mut self.wavelet].
// In the original setup callback, after dependency-first routing:
let frozen = self.wavelet.snapshot_at("MSFT", frame.available_at_ns);
// Store frozen WITH this setup. Later outcome calculation reads this snapshot,
// never a later Indicator::value(). Historical snapshot_at uses availability
// bounded by the supplied original decision time, including missing/warm-up.
```

Snapshots retain decision time, availability, trailing-window start, coarse energy
share, signed half-window trend in bps, and detail energies by support. Energy is
unsigned; do not infer direction from it. Missing bars reset warm-up; flat windows
have undefined energy share. Symbols are isolated and slower features update only
on their declared completed cadence. Constructor parameters are research choices
to explore, not default values known to work.

Managed snapshot selection cannot return an observation from after its as-of time.
Retaining a snapshot prevents later bars from changing its values. Arbitrary authored
Rust can still fabricate timestamps, call callbacks directly or compute other
features; this SDK path does not sandbox all possible scientific misrepresentation.
Critique must verify that the policy actually uses the managed observation path.

## Training strategy evaluation

Use author_strategy with parameter_declaration and validate_strategy, then
run_backtest/run_backtest_batch on the development window. Diagnostic studies
do not replace policy evaluation. Retain all candidates and failed attempts.
run_null_batch supplies fixed-assignment conditional null evidence; see
[Testing a searched candidate against a null](testing-a-searched-candidate-against-a-null)
before making search-adjusted claims.

## Explicit fit and frozen inference recipe

Collect a declared development-only feature window and call
`FittedNormalization::fit(&training_values)`. Publish `StudyOutput::FittedState`
with method `population_normalization`, training start/end, availability, seed
`0`, configuration, and weights `mean` and `scale`. The host assigns the actual
`training_job_id` and validates the training window against executed data;
authors do not supply or invent job provenance. The job owns the persisted
artifact. Pin its immutable job/artifact reference in
the sketch and inject the exact recorded coefficients into the later program's
parameters. Construct `FittedNormalization { mean, scale }` and call `transform`
on subsequent observations. Never fit on forward data, silently update weights,
or claim an earlier availability than the training window end. Report warm-up,
fit failure, drift, and the possibility that unusual activity has no directional
or economic value.
