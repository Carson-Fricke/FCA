---
id: executable-discovery-tool-bag
title: Executable discovery tool bag and composition recipes
version: 1
created: 2026-10-09
retrieved: 2026-10-09
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 30e6e67e2ebb8054fb8b4b54b23b441dc9dcfb4a223c2ef1a31d45587399245c
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
  - directional-change-events
  - perceptually-important-points
  - discovery-study-design
  - historical-timing-and-leakage
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
| `trailing_haar` | Power-of-two trailing values → multiscale detail coefficients | Scale-specific compression and release | Haar basis; no future padding or circular boundary; not MODWT/CWT |
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

## Executable study with a sequence, confirmed events, and a wavelet table

This complete study illustrates composition and persisted exploratory output.
Replace the symbol, parameters, and representation to investigate your sketch.
Author, validate, and run the committed study through the standard program tools.
Inspect the originating job artifacts; reference that job in sketch evidence.

```rust
use std::collections::{BTreeMap, VecDeque};
use harness_strategy_sdk::{export_study_program, serde_json::{self, Value},
    BarFrame, MarketConsumer, MetricPublisher, ObservationContext, OutputEvent,
    OutputPoint, StudyOutput, StudyProgram, Subscription};
use harness_strategy_sdk::building_blocks::{DirectionalChange, RollingMoments, trailing_haar};

struct Exploration {
    symbol: String, interval: String, dc: DirectionalChange,
    moments: RollingMoments, window: VecDeque<f64>,
    points: Vec<OutputPoint>, events: Vec<OutputEvent>, rows: Vec<Vec<Value>>,
}
impl StudyProgram for Exploration {
    fn new(p: Value) -> Self {
        Self {
            symbol: p["symbol"].as_str().unwrap_or("MSFT").into(),
            interval: p["interval"].as_str().unwrap_or("1min").into(),
            dc: DirectionalChange::new(p["threshold"].as_f64().unwrap_or(1.0)).unwrap(),
            moments: RollingMoments::new(32).unwrap(), window: VecDeque::new(),
            points: vec![], events: vec![], rows: vec![],
        }
    }
    fn on_stop(&mut self, out: &mut MetricPublisher) {
        out.publish_output("dispersion", &StudyOutput::Series { points: self.points.clone() }).unwrap();
        out.publish_output("release_events", &StudyOutput::Events { events: self.events.clone() }).unwrap();
        out.publish_output("wavelet_energy", &StudyOutput::Table {
            columns: vec!["available_at_ns".into(), "support".into(), "energy".into()],
            rows: self.rows.clone(),
        }).unwrap();
    }
}
impl MarketConsumer for Exploration {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Bars { symbols: vec![self.symbol.clone()], interval: self.interval.clone() }]
    }
    fn on_bar(&mut self, frame: &BarFrame, _: &mut ObservationContext) {
        let Some(bar) = frame.bars_by_symbol.get(self.symbol.as_str()) else { return; };
        let price = bar.close.to_f64();
        let dispersion = self.moments.observe(price).unwrap().map(|(_,sd)| sd);
        self.points.push(OutputPoint { symbol: self.symbol.clone(), interval: self.interval.clone(),
            event_time_ns: frame.interval_end_ns, available_at_ns: frame.available_at_ns,
            value: dispersion, undefined_reason: dispersion.is_none().then(|| "warm_up".into()) });
        if let Some(event) = self.dc.observe(frame.available_at_ns, price).unwrap() {
            self.events.push(OutputEvent { symbol: self.symbol.clone(),
                event_time_ns: event.extreme_time_ns, available_at_ns: event.available_at_ns,
                fields: BTreeMap::from([("direction".into(), serde_json::json!(event.direction)),
                    ("confirmation_price".into(), serde_json::json!(event.price))]) });
        }
        self.window.push_back(price);
        if self.window.len() > 32 { self.window.pop_front(); }
        if self.window.len() == 32 {
            for level in trailing_haar(&self.window.iter().copied().collect::<Vec<_>>()).unwrap() {
                let energy = level.details.iter().map(|x| x*x).sum::<f64>() / level.details.len() as f64;
                self.rows.push(vec![serde_json::json!(frame.available_at_ns),
                    serde_json::json!(level.support), serde_json::json!(energy)]);
            }
        }
    }
}
export_study_program!(Exploration);
```

For discontinuous input, define whether to reset the window or retain observations
and record that choice. This example counts observed bars and omits missing bars;
it does not manufacture observations or infer unseen crossings.

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
