//! Indicator sequence behavior at the public market-routing boundary.
use harness_strategy_sdk::route_bar;
use harness_strategy_sdk::route_quote;
use harness_strategy_sdk::route_trade;
use harness_strategy_sdk::BarEvent;
use harness_strategy_sdk::BarFrame;
use harness_strategy_sdk::Decimal;
use harness_strategy_sdk::Indicator;
use harness_strategy_sdk::IndicatorMissing;
use harness_strategy_sdk::IndicatorSequence;
use harness_strategy_sdk::MarketConsumer;
use harness_strategy_sdk::ObservationContext;
use harness_strategy_sdk::QuoteEvent;
use harness_strategy_sdk::RoutingPlan;
use harness_strategy_sdk::Subscription;
use harness_strategy_sdk::TradeEvent;
use std::collections::BTreeMap;

struct Values {
    sequence: IndicatorSequence,
    values: BTreeMap<String, Option<Decimal>>,
    ticks: bool,
}
impl MarketConsumer for Values {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        let symbols = vec!["AAA".into(), "BBB".into()];
        if self.ticks {
            vec![
                Subscription::Trade {
                    symbols: symbols.clone(),
                },
                Subscription::Quote { symbols },
            ]
        } else {
            vec![Subscription::Bars {
                symbols,
                interval: "1min".into(),
            }]
        }
    }
    fn on_trade(&mut self, event: &TradeEvent, _: &mut ObservationContext) {
        self.values.insert(event.symbol.into(), Some(event.price));
    }
    fn on_quote(&mut self, event: &QuoteEvent, _: &mut ObservationContext) {
        self.values.insert(event.symbol.into(), Some(event.bid));
    }
    fn on_bar(&mut self, frame: &BarFrame, _: &mut ObservationContext) {
        for (symbol, bar) in &frame.bars_by_symbol {
            self.values.insert(
                (*symbol).into(),
                if bar.close == Decimal::ZERO {
                    None
                } else {
                    Some(bar.close)
                },
            );
        }
    }
}
impl Indicator for Values {
    fn value(&self, symbol: &str) -> Option<Decimal> {
        self.values.get(symbol).copied().flatten()
    }
    fn sequence(&self) -> &IndicatorSequence {
        &self.sequence
    }
    fn sequence_mut(&mut self) -> &mut IndicatorSequence {
        &mut self.sequence
    }
}
struct Owner {
    indicator: Values,
}
impl MarketConsumer for Owner {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![]
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        vec![&mut self.indicator]
    }
}
fn owner(ticks: bool) -> Owner {
    Owner {
        indicator: Values {
            ticks,
            values: BTreeMap::new(),
            sequence: if ticks {
                IndicatorSequence::ticks(10).unwrap()
            } else {
                IndicatorSequence::default()
            },
        },
    }
}
fn trade(symbol: &str, time: i64, price: i128) -> TradeEvent<'_> {
    TradeEvent {
        event_id: "t",
        timestamp_ns: time,
        sequence: 1,
        symbol,
        price: Decimal::from_i128(price),
        quantity: Decimal::from_i128(1),
        venue: "test",
        trade_id: "t",
    }
}
#[test]
fn trades_and_quotes_produce_symbol_bound_ohlc_at_requested_fidelity() {
    let mut owner = owner(true);
    let mut owner_plan = RoutingPlan::new(&mut owner).unwrap();
    route_trade(
        &mut owner,
        &trade("AAA", 1, 4),
        &mut ObservationContext,
        &mut owner_plan,
    )
    .unwrap();
    route_trade(
        &mut owner,
        &trade("BBB", 2, 99),
        &mut ObservationContext,
        &mut owner_plan,
    )
    .unwrap();
    let quote = QuoteEvent {
        event_id: "q",
        timestamp_ns: 3,
        sequence: 2,
        symbol: "AAA",
        bid: Decimal::from_i128(2),
        ask: Decimal::from_i128(3),
        bid_quantity: Decimal::ZERO,
        ask_quantity: Decimal::ZERO,
        venue: "test",
    };
    route_quote(&mut owner, &quote, &mut ObservationContext, &mut owner_plan).unwrap();
    route_trade(
        &mut owner,
        &trade("AAA", 4, 8),
        &mut ObservationContext,
        &mut owner_plan,
    )
    .unwrap();
    assert!(owner.indicator.sequence().completed().is_empty());
    // An observed global time seals populated windows even without new input for that symbol.
    route_trade(
        &mut owner,
        &trade("UNREQUESTED", 10, 100),
        &mut ObservationContext,
        &mut owner_plan,
    )
    .unwrap();
    let bars = owner.indicator.sequence().completed();
    let bar = &bars[&("AAA".into(), "ticks:10".into())][0];
    assert_eq!(
        (bar.start_ns, bar.end_ns, bar.available_at_ns),
        (0, 10, Some(10))
    );
    assert_eq!(
        (bar.open, bar.high, bar.low, bar.close),
        (
            Some(Decimal::from_i128(4)),
            Some(Decimal::from_i128(8)),
            Some(Decimal::from_i128(2)),
            Some(Decimal::from_i128(8))
        )
    );
    assert_eq!(bar.observations, 3);
    assert_eq!(
        bars[&("BBB".into(), "ticks:10".into())][0].close,
        Some(Decimal::from_i128(99))
    );
    route_trade(
        &mut owner,
        &trade("AAA", 35, 7),
        &mut ObservationContext,
        &mut owner_plan,
    )
    .unwrap();
    assert_eq!(owner.indicator.sequence().completed().len(), 2);
    assert_eq!(owner.indicator.sequence().in_progress()["AAA"].start_ns, 30);
    assert!(route_trade(
        &mut owner,
        &trade("AAA", 34, 6),
        &mut ObservationContext,
        &mut owner_plan
    )
    .is_err());
    assert_eq!(owner.indicator.value("AAA"), Some(Decimal::from_i128(7)));
}
fn frame(start: i64, close: i128, missing: bool) -> BarFrame<'static> {
    let bar = BarEvent {
        event_id: "b",
        timestamp_ns: start,
        sequence: 1,
        symbol: "AAA",
        open: Decimal::ZERO,
        high: Decimal::ZERO,
        low: Decimal::ZERO,
        close: Decimal::from_i128(close),
        volume: Decimal::ZERO,
    };
    BarFrame {
        interval: "1min",
        interval_start_ns: start,
        interval_end_ns: start + 10,
        available_at_ns: start + 12,
        bars_by_symbol: if missing {
            BTreeMap::new()
        } else {
            [("AAA", bar)].into()
        },
        missing_symbols: if missing {
            vec!["AAA", "BBB"]
        } else {
            vec!["BBB"]
        },
    }
}
#[test]
fn bar_outputs_preserve_boundaries_warmup_and_missing_without_carry_forward() {
    let mut owner = owner(false);
    let mut owner_plan = RoutingPlan::new(&mut owner).unwrap();
    route_bar(
        &mut owner,
        &frame(0, 0, false),
        &mut ObservationContext,
        &mut owner_plan,
    )
    .unwrap();
    route_bar(
        &mut owner,
        &frame(10, 5, false),
        &mut ObservationContext,
        &mut owner_plan,
    )
    .unwrap();
    route_bar(
        &mut owner,
        &frame(20, 0, true),
        &mut ObservationContext,
        &mut owner_plan,
    )
    .unwrap();
    let bars = &owner.indicator.sequence().completed()[&("AAA".into(), "1min".into())];
    assert_eq!(bars.len(), 3);
    assert_eq!(bars[0].missing, Some(IndicatorMissing::Undefined));
    assert_eq!(bars[1].close, Some(Decimal::from_i128(5)));
    assert_eq!(bars[2].missing, Some(IndicatorMissing::MissingInput));
    assert_eq!(bars[2].close, None);
    assert_eq!(
        (bars[2].start_ns, bars[2].end_ns, bars[2].available_at_ns),
        (20, 30, Some(32))
    );
    assert!(owner.indicator.sequence().in_progress().is_empty());
}
#[test]
fn tick_fidelity_must_be_explicit_and_positive() {
    assert!(IndicatorSequence::ticks(0).is_err());
    assert!(IndicatorSequence::ticks(-1).is_err());
    let mut owner = owner(true);
    owner.indicator.sequence = IndicatorSequence::default();
    assert!(harness_strategy_sdk::RoutingPlan::new(&mut owner)
        .err()
        .unwrap()
        .contains("fidelity"));
}
