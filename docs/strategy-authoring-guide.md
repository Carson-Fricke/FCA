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
the engine's strategy runtime. Strategies are **tick-based**: they receive
top-of-book quotes and trade prints, submit orders, and observe their own
fills. (Studies fold bars only; screening programs evaluate one cross-section.
Neither is a strategy.)

The SDK owns the ABI. The loader rejects an artifact whose SDK ABI version
differs from the host's, so an authored strategy never silently links against a
mismatched runtime. One authored crate exports exactly one program.

## 2. Program contract

```rust
pub trait StrategyProgram {
    fn new(parameters: serde_json::Value) -> Self where Self: Sized;
    fn on_start(&mut self, start: &ProgramStart) {}
    fn on_quote(&mut self, event: &QuoteEvent, orders: &mut OrderSink) {}
    fn on_trade(&mut self, event: &TradeEvent, orders: &mut OrderSink) {}
    fn on_fill(&mut self, fill: &Fill) {}
    fn on_stop(&mut self, metrics: &mut MetricPublisher) {}
}
```

- `new` receives the configured JSON `parameters` exactly as submitted.
- `on_start` runs once before the first market event and carries the instance
  id, account id, universe symbols, run seed, initial cash, and start time.
- `on_quote`/`on_trade` fold market events and are the only places that submit
  orders.
- `on_fill` folds a fill owned by this program.
- `on_stop` runs once after the replay and is the only place that publishes
  metrics.

Every callback has a no-op default, so a strategy implements only what it uses.

## 3. Events and the capability surface

Event fields are borrowed, so there is no per-event allocation, and every price
and quantity is an exact `Decimal` (never `f64`):

- `QuoteEvent` — `event_id`, `timestamp_ns`, `sequence`, `symbol`, `bid`, `ask`,
  `bid_quantity`, `ask_quantity`, `venue`, plus `mid()`.
- `TradeEvent` — `event_id`, `timestamp_ns`, `sequence`, `symbol`, `price`,
  `quantity`, `venue`, `trade_id`, plus `notional()`.
- `Fill` — `event_id`, `timestamp_ns`, `sequence`, `fill_id`, `order_id`,
  `symbol`, `side`, `quantity`, `price`, `fee`, `liquidity`, plus
  `signed_quantity()`.
- `ProgramStart` — `instance_id`, `account_id`, `symbols`, `seed`,
  `initial_cash`, `start_ns`.

The SDK's engine-facing surface is the event, `OrderSink`, and
`MetricPublisher`. This is not a security sandbox: native Rust can still call
standard-library filesystem, network, clock, process, and threading APIs.
Structural authoring checks do not prevent those operations, and successful
JIT compilation is not proof of isolation. Strategies must follow the
determinism rules below; hostile source requires a separate execution sandbox.

## 4. Orders

Submit through `OrderSink::submit` with a `SubmittedOrder`. The host stamps the
event time; the author never supplies a timestamp.

```rust
let _ = orders.submit(SubmittedOrder {
    order_id: format!("mom-buy-{}", event.sequence),
    symbol: event.symbol.to_string(),
    side: Side::Buy,
    quantity: "10".parse().unwrap_or_default(),
    order_type: OrderType::Market,
    time_in_force: TimeInForce::Day,
    limit_price: None,
    stop_price: None,
    sequence: event.sequence,
    event_id: format!("mom-buy-event-{}", event.sequence),
});
```

- `Side` is `Buy`/`Sell`; `OrderType` is `Market`/`Limit`/`Stop`/`StopLimit`;
  `TimeInForce` is `Day`/`Gtc`/`Ioc`.
- `limit_price` is required for `Limit`/`StopLimit`; `stop_price` for
  `Stop`/`StopLimit`; both are `None` for `Market`.
- `order_id` must be unique and deterministic. A prefix plus a sequence-derived
  ordinal keeps restarts reproducible.
- Use exact decimals. Parse from strings (`"10".parse()`), never from a float.

## 5. Metrics

`MetricPublisher::publish(name, value)` records one finite numeric run metric.
Names must match `^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$` and must not be a
reserved harness name (`cagr`, `fees_total`, `fill_count`, `max_drawdown`,
`slippage_total`, `strategy_id`, `total_return`). A rejection is recorded
host-side and fails the run even when the return value is ignored.

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
use harness_strategy_sdk::{
    export_strategy_program, Decimal, Fill, MetricPublisher, OrderSink, OrderType, QuoteEvent,
    Side, StrategyProgram, SubmittedOrder, TimeInForce,
};

struct Momentum {
    streak: u64,
    last_mid: Option<Decimal>,
    entered: bool,
    filled: bool,
    liquidated: bool,
}

impl StrategyProgram for Momentum {
    fn new(_parameters: Value) -> Self {
        Self { streak: 0, last_mid: None, entered: false, filled: false, liquidated: false }
    }

    fn on_quote(&mut self, event: &QuoteEvent, orders: &mut OrderSink) {
        let mid = event.mid();
        if let Some(previous) = self.last_mid {
            self.streak = if mid.cmp_value(previous) == Ordering::Greater { self.streak + 1 } else { 0 };
        }
        self.last_mid = Some(mid);
        if !self.entered && self.streak >= 4 {
            self.entered = true;
            let _ = orders.submit(SubmittedOrder {
                order_id: format!("mom-buy-{}", event.sequence),
                symbol: event.symbol.to_string(),
                side: Side::Buy,
                quantity: "10".parse().unwrap_or_default(),
                order_type: OrderType::Market,
                time_in_force: TimeInForce::Day,
                limit_price: None,
                stop_price: None,
                sequence: event.sequence,
                event_id: format!("mom-buy-event-{}", event.sequence),
            });
        } else if self.entered && self.filled && !self.liquidated {
            self.liquidated = true;
            let _ = orders.submit(SubmittedOrder {
                order_id: format!("mom-sell-{}", event.sequence),
                symbol: event.symbol.to_string(),
                side: Side::Sell,
                quantity: "10".parse().unwrap_or_default(),
                order_type: OrderType::Market,
                time_in_force: TimeInForce::Day,
                limit_price: None,
                stop_price: None,
                sequence: event.sequence,
                event_id: format!("mom-sell-event-{}", event.sequence),
            });
        }
    }

    fn on_fill(&mut self, _fill: &Fill) {
        self.filled = true;
    }

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("momentum_liquidated", f64::from(self.liquidated));
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
Run the repository Rust gates from the repository root:

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
2. Every price and quantity is built from `str`/`i128`/`Decimal`, never `f64`.
3. Do not use wall clocks, networks, subprocesses, threads, or unseeded
   randomness. These are authoring rules, not compile-time Rust restrictions.
4. `order_id` values are deterministic and unique for the instance.
5. `on_fill` handles only fills this program owns.
6. A study or screening capability is never used from a strategy, and vice
   versa; the three program kinds are separate traits and vtables.
