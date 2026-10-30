# Strategy Authoring Guide

Status: current. Strategies are **pure Rust**, authored against the
`harness-strategy-sdk` crate and JIT-compiled by the harness through
`harness-strategy-jit`. This guide replaces the retired embedded-Python
authoring guide; there is no Python or JavaScript strategy surface.

A strategy is also a **workspace artifact**: its source, tests, and
`manifest.json` live under `<workspace>/strategies/<slug>/<version>/` and are
served to the Strategies page as a projection. It is never stored in Cosmos.
See `.github/architecture/strategy-authoring-plan.md` for the authoring path,
`.github/architecture/repository.md` ("Program Authoring Contract") for the
implemented harness contract, and
`.github/architecture/rust-native-authoring-plan.md` for the SDK/JIT migration.

## 1. What a strategy is

A strategy is one Rust source file (a small crate) that implements the
`StrategyProgram` trait and exports itself with `export_strategy_program!`. It
is compiled on submit, loaded through the versioned C ABI, and driven through
the engine's strategy runtime. Studies, strategies and authored indicators all
implement `MarketConsumer`: subscribed top-of-book quotes, trade prints and
atomic bar frames use the same callbacks. Strategies alone have an `OrderSink`
context and observe their own fills. Studies and indicators use
`ObservationContext`, which has no order capability. Screening programs still
evaluate a separate cross-sectional context.

The SDK owns the ABI. The loader rejects an artifact whose SDK ABI version
differs from the host's, so an authored strategy never silently links against a
mismatched runtime. One authored crate exports exactly one program.

## 2. Program contract

```rust
pub trait MarketConsumer {
    type Context;
    fn subscriptions(&self) -> Vec<Subscription>;
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> { vec![] }
    fn on_quote(&mut self, event: &QuoteEvent, ctx: &mut Self::Context) {}
    fn on_trade(&mut self, event: &TradeEvent, ctx: &mut Self::Context) {}
    fn on_bar(&mut self, frame: &BarFrame, ctx: &mut Self::Context) {}
    fn on_decision(&mut self, ctx: &mut DecisionContext<'_>) {}
}

pub trait StrategyProgram: MarketConsumer<Context = OrderSink> {
    fn new(parameters: serde_json::Value) -> Self where Self: Sized;
    fn on_start(&mut self, start: &ProgramStart) {}
    fn on_fill(&mut self, fill: &Fill) {}
    fn on_stop(&mut self, metrics: &mut MetricPublisher) {}
}

pub trait StudyProgram: MarketConsumer<Context = ObservationContext> {
    fn new(parameters: serde_json::Value) -> Self where Self: Sized;
    fn on_stop(&mut self, metrics: &mut MetricPublisher) {}
}

pub trait Indicator: MarketConsumer<Context = ObservationContext> {
    fn value(&self, symbol: &str) -> Option<Decimal>;
    fn sequence(&self) -> &IndicatorSequence;
    fn sequence_mut(&mut self) -> &mut IndicatorSequence;
}
```

- `new` receives the configured JSON `parameters` exactly as submitted.
- `on_start` runs once before the first market event and carries the instance
  id, account id, universe symbols, run seed, initial cash, and start time.
- `on_quote`/`on_trade`/`on_bar` belong to `MarketConsumer`; strategy callbacks
  may submit orders through their context. Bar callbacks see complete intervals
  at interval end, never bar-start lookahead.
- `on_fill` optionally observes a fill already booked by the harness. Use the
  execution context for positions, cash, fees and returns; implement no fill ledger.
- `on_stop` runs once after the replay and is the only place that publishes
  metrics.
- `on_decision` runs on the program root after each effective bar frame has
  updated its dependencies and ordinary callback. Capture causal features here.

Every callback has a no-op default, so a strategy implements only what it uses.
Keep `new`, `on_start`, `on_fill`, and `on_stop` in the `StrategyProgram` impl;
market methods belong in its separate `MarketConsumer` impl. Studies likewise
keep `new` and `on_stop` in `StudyProgram`; `on_bar` receives a `BarFrame`, not
a raw single bar. SDK indicators expose their current value and references to
owned nested indicators; the native built-in indicator registry has not
migrated to this authored SDK interface.

Subscriptions are tagged as `bars` (symbols plus interval `1min` or `1d`),
`quote` (symbols), or `trade` (symbols). Requirements of program-owned
indicators are recursively unioned and deduplicated before data acquisition.
Each dependency is updated before its parent and receives only its own inputs.
Declarations and dependency topology must remain stable after construction;
cycles and zero-sized indicator dependencies fail validation. A run supports multiple declared bar intervals; keep symbol and cadence state
separate and use only observations already available at the decision time.

Studies fetch only their effective subscription union. Backtests additionally
fetch quote/trade feeds needed by the execution model; these are recorded
separately as execution subscriptions. An optional `data.subscriptions` echo
must match compiled consumer requirements, not execution feeds.

ABI v8 exposes the constructed source-and-dependency union as tagged JSON before
any lake ensure/read. Thesis authors `harnessConfiguration.subscriptions`,
preserves it as `data.subscriptions`, and uses `validate_study` to return the
compiled union and reject canonically unequal echoes. Entries have exactly
`data_type` and `symbols`, with `interval` only for `bars`; unknown fields and
tick intervals are rejected. Optional global symbols must equal the subscription
symbol union. Tick-only inputs need no interval. Interval-only configuration is
rejected; authored source uses the current SDK API and artifacts build for
ABI v8.

The manifest distinguishes `effective_subscriptions` (source plus dependencies),
`execution_subscriptions` (backtest quote/trade feeds), and
`acquired_subscriptions` (their deduplicated union). Bars ensures do not fetch
undeclared trades: they use provider bars or already stored trades. Mixed replay
uses one causal availability queue; ticks precede completed frames, then
generated execution events at equal time. Frames are available at interval end.

## 3. Events and the capability surface

For feature/outcome evidence, use the shared `DecisionContext` in studies and
strategies. Read `read_sdk_reference(topic="causal")` against the serving SDK.
The study template demonstrates the current API:

```rust
fn on_decision(&mut self, context: &mut DecisionContext<'_>) {
    // self.features was computed by causal on_bar callbacks and dependencies.
    context.capture("signal", "SPY", self.features.clone(), 60_000_000_000)
        .expect("host-sealed features");
}
```

Dataset and feature names contain 1–64 ASCII letters, digits, `.`, `_` or `-`.
Pass an unprefixed dataset name such as `signal`; the host publishes
`causal/signal`. Capture 1–256 finite features, skip warm-up, and keep each
dataset's feature names and positive nanosecond horizon fixed. A rejection fails
the run even if its return value is ignored; the job failure identifies the
violated field. The same dataset/symbol can be captured only once per frame.

The harness copies finite features immediately and assigns the observation ID,
decision availability, symbol and cadence from the active frame. There is no
timestamp argument, update operation, outcome reader or capture port at stop.
One dataset/symbol capture per frame is allowed; its feature schema and positive
elapsed horizon remain fixed. Warm-up may skip capture. An ignored capture
rejection still fails the run. Outcomes use the same symbol and cadence at
exactly `decision_at_ns + horizon_ns`; they are close-to-close returns in bps,
without execution costs. Gaps are `missing`, and remaining horizons at stop are
`end_of_run`, with null returns. Later bars never substitute for missing ones.
An elapsed day is not a trading-session horizon; overnight and weekend gaps
remain missing under this contract.

Only the harness publishes `causal/<dataset>` as `causal_observations`. These
outputs contain decimal-string nanosecond clocks, immutable `feature/*` columns,
reference/outcome prices, outcome availability and explicit outcome status.
Research views and dataset semantics can reference them. Authored publications
cannot use this kind or namespace. The inspector marks these outputs as
`harness_replay`; ordinary tables, series, fitted state and metrics remain
authored exploratory claims. A retrospective join cannot acquire sealed
provenance by declaring earlier availability. SDK ABI 8 requires compiled
artifacts to rebuild; the loader rejects mismatched artifacts rather than
decoding an old layout. Existing exploratory artifacts remain exploratory.

This enforces causality for supported replay/capture inputs. Native Rust is not
a security sandbox: hard-coded future knowledge, external I/O, incorrect source
availability, training leakage and research selection bias require separate
controls. Raw post-run tables do not prove causal backtest results.

Event string fields are borrowed across the ABI; frame maps are constructed
locally by the SDK. Every price and quantity is an exact `Decimal` (never `f64`):

- `QuoteEvent` — `event_id`, `timestamp_ns`, `sequence`, `symbol`, `bid`, `ask`,
  `bid_quantity`, `ask_quantity`, `venue`, plus `mid()`.
- `TradeEvent` — `event_id`, `timestamp_ns`, `sequence`, `symbol`, `price`,
  `quantity`, `venue`, `trade_id`, plus `notional()`.
- `Fill` — `event_id`, `timestamp_ns`, `sequence`, `fill_id`, `order_id`,
  `symbol`, `side`, `quantity`, `price`, `fee`, `liquidity`, plus
  `signed_quantity()`.
- `ProgramStart` — `instance_id`, `account_id`, `symbols`, `seed`,
  `initial_cash`, `start_ns`.
- `BarFrame` contains `interval`, `interval_start_ns`, `interval_end_ns`,
  `available_at_ns`, exact OHLCV `bars_by_symbol`, and `missing_symbols`.
  Handle missing symbols explicitly; never align by row position, forward-fill,
  or read a complete bar at its start time.

The SDK's engine-facing surface is the event, `OrderSink`, and
`MetricPublisher`. This is not a security sandbox: native Rust can still call
standard-library filesystem, network, clock, process, and threading APIs.
Structural authoring checks do not prevent those operations, and successful
JIT compilation is not proof of isolation. Strategies must follow the
determinism rules below; hostile source requires a separate execution sandbox.

Decimals stay exact through the data, order, fill, fee, and cash surfaces.
Analytics and metrics are `f64`: convert explicitly and once at that boundary
with `Decimal::to_f64` (nearest, ties to even) or the fail-loud
`Decimal::to_f64_checked`, which returns `Err(DecimalError::Overflow)` when the
magnitude is outside the `f64` range. `f64::try_from(decimal)` is the same
checked conversion. Never build a `Decimal` from a float.

## 4. Orders

Use managed signed inventory targets for entry and exit. The harness owns
partial fills, pending orders, cancellations, actual position size and accounting:

```rust
ctx.target_position("MSFT", Decimal::from_i128(10)).expect("target request");
// When the policy chooses to exit:
ctx.close_position("MSFT").expect("close request");
```

Repeated targets are idempotent while orders are pending. For explicit execution
policy, `ctx.place(Order::market/limit/stop/stop_limit(...))` supplies required
prices and allocates the order identity. `ctx.cancel(id)` cancels an owned order.
Time-in-force policies are `Day`, `Gtc` and `Ioc`. The host stamps causal time.
`SubmittedOrder` remains a low-level surface for specialized intent producers;
ordinary strategies do not construct identifiers or event sequences themselves.

`ctx.execution()` supplies booked positions, average price, cash, fees, equity
and exposure. `ctx.orders()` / `ctx.order(id)` supply lifecycle, and
`ctx.closed_trades()` supplies canonical FIFO trade P&L and entry-notional returns.
These history views are loaded separately on demand. Final views are available
through `MetricPublisher` in `on_stop`. See [Strategy execution SDK](strategy-execution-sdk.md)
for cancellation races, rejection, return denominators and accounting conventions.

## 5. Metrics

`MetricPublisher::publish(name, value)` records one finite numeric run metric.
Names must match `^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$` and must not be a
reserved harness name (see `is_reserved_metric_name` and `RESERVED_METRIC_NAMES`),
including canonical financial values and their undefined-reason fields. A rejection is recorded
host-side and fails the run even when the return value is ignored.

Metrics take `f64`; exact decimals convert at publish time:

```rust
let mean_close = close_sum.to_f64() / bars as f64;
let _ = metrics.publish("mean_close", mean_close);
```

Use `to_f64_checked` (or `f64::try_from`) when a non-finite conversion must
fail the run instead of saturating to infinity.

## 6. Export

```rust
export_strategy_program!(MyStrategy);
```

This generates the handshake symbols (`harness_sdk_abi_version`,
`harness_sdk_last_error`, `harness_sdk_strategy_vtable`) and the vtable
trampolines. Every callback runs inside `catch_unwind`; a panic is contained and
reported as a strategy-execution error rather than unwinding across the ABI.

## 7. Worked example

A minimal momentum strategy: enter after four consecutively rising quote
midpoints, liquidate once the entry has filled.

```rust
use std::cmp::Ordering;

use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{export_strategy_program, Decimal, MarketConsumer,
    OrderSink, QuoteEvent, StrategyProgram, Subscription};

struct Momentum {
    streak: u64,
    last_mid: Option<Decimal>,
    activated: bool,
    closing: bool,
}

impl StrategyProgram for Momentum {
    fn new(_parameters: Value) -> Self {
        Self { streak: 0, last_mid: None, activated: false, closing: false }
    }
}

impl MarketConsumer for Momentum {
    type Context = OrderSink;

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Quote { symbols: vec!["SPY".into()] }]
    }

    fn on_quote(&mut self, event: &QuoteEvent, orders: &mut OrderSink) {
        let mid = event.mid();
        if let Some(previous) = self.last_mid {
            self.streak = if mid.cmp_value(previous) == Ordering::Greater { self.streak + 1 } else { 0 };
        }
        self.last_mid = Some(mid);
        if self.streak >= 4 { self.activated = true; }
        if orders.execution().quantity(event.symbol) == Decimal::from_i128(10) {
            self.closing = true;
        }
        if self.closing {
            orders.close_position(event.symbol).expect("close request");
        } else if self.activated {
            orders.target_position(event.symbol, Decimal::from_i128(10)).expect("entry target");
        }
    }

}

export_strategy_program!(Momentum);
```

## 8. Submission configuration

The strategy is carried by a harness configuration. The instance names the
JIT implementation and the source travels inline or by path:

```yaml
strategy_instances:
  - instance_id: spy-momentum
    implementation: jit
    bundle_hash: "<sha256 of the authored source>"
    capital_fraction: 1.0
    symbols: [SPY]
    parameters:
      source: |
        use harness_strategy_sdk::serde_json::Value;
        // ... the strategy source above ...
```

- `implementation` must be `"jit"`.
- The authored source travels as `parameters.source` (inline, for JSON
  configurations) or as `parameters.source_path` (a file resolved relative to
  the configuration document).
- `bundle_hash` must equal the SHA-256 of the authored source; the JIT seam
  rejects a mismatch.

## 9. Local verification

An authored strategy is a Rust crate compiled against the pinned toolchain.
Run the harness Rust gates from `src/harness`:

```powershell
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

For the SDK-level contract, the `strategy-jit` suite proves study, strategy, and
screening runs through the real job seam, the capability gate, panic
containment, and metric rejection handling.

## 10. Determinism and capability rules

1. The only engine access is the event, the `OrderSink`, and the
   `MetricPublisher`.
2. Every price and quantity is built from `str`/`i128`/`Decimal`, never `f64`;
   decimals convert to `f64` only at the metric or statistic boundary.
3. Do not use wall clocks, networks, subprocesses, threads, or unseeded
   randomness. These are authoring rules, not compile-time Rust restrictions.
4. The platform supplies deterministic order identities through `place` and targets.
5. The harness accounts for fills; `on_fill` observes only fills this program owns.
6. A study or screening capability is never used from a strategy, and vice
   versa; the three program kinds are separate traits and vtables.
