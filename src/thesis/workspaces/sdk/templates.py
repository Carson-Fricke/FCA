"""Minimal SDK consumer source assets; no workspace or execution policy."""

STUDY_TEMPLATE = """use std::collections::BTreeMap;
use harness_strategy_sdk::{export_study_program, BarFrame, DecisionContext, MarketConsumer,
    MetricPublisher, ObservationContext, StudyProgram, Subscription};
use harness_strategy_sdk::serde_json::Value;
#[derive(Default)]
struct AuthoredStudy { frames: u64, close: BTreeMap<String, f64> }
impl MarketConsumer for AuthoredStudy {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Bars { symbols: vec!["SPY".into()], interval: "1min".into() }]
    }
    fn on_bar(&mut self, frame: &BarFrame, _: &mut ObservationContext) {
        self.frames += 1;
        self.close = frame.bars_by_symbol.iter().map(|(symbol, bar)|
            ((*symbol).into(), bar.close.to_f64_checked().expect("finite close"))).collect();
    }
    fn on_decision(&mut self, context: &mut DecisionContext<'_>) {
        for (symbol, close) in &self.close {
            context.capture("close", symbol, BTreeMap::from([("close".into(), *close)]),
                60_000_000_000).expect("host-sealed observation");
        }
    }
}
impl StudyProgram for AuthoredStudy {
    fn new(_: Value) -> Self { Self::default() }
    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("study_frames", self.frames as f64);
    }
}
export_study_program!(AuthoredStudy);
"""
STRATEGY_TEMPLATE = """use harness_strategy_sdk::{export_strategy_program, BarFrame, MarketConsumer,
    OrderSink, StrategyProgram, Subscription};
use harness_strategy_sdk::serde_json::Value;
struct AuthoredStrategy;
impl MarketConsumer for AuthoredStrategy {
    type Context = OrderSink;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Bars { symbols: vec!["SPY".into()], interval: "1min".into() }]
    }
    fn on_bar(&mut self, _: &BarFrame, ctx: &mut OrderSink) {
        // Add signal policy here. The harness owns positions, partial fills,
        // order lifecycle, fees and P&L: inspect ctx.execution().
        // ctx.target_position(symbol, quantity) maintains signed inventory;
        // ctx.close_position(symbol) cancels working exposure and flattens.
        // ctx.place(Order::limit(...)) uses platform-owned order identities.
        let _account = ctx.execution();
    }
}
impl StrategyProgram for AuthoredStrategy {
    fn new(_: Value) -> Self { Self }
}
export_strategy_program!(AuthoredStrategy);
"""

INDICATOR_TEMPLATE = """use std::collections::BTreeMap;
use harness_strategy_sdk::{BarFrame, Decimal, Indicator, IndicatorSequence, MarketConsumer,
    ObservationContext, QuoteEvent, Subscription, TradeEvent};
#[derive(Default)]
pub struct AuthoredIndicator {
    last: BTreeMap<String, Option<Decimal>>, sequence: IndicatorSequence
}
impl MarketConsumer for AuthoredIndicator {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Bars { symbols: vec!["SPY".into()], interval: "1min".into() }]
    }
    fn on_bar(&mut self, frame: &BarFrame, _: &mut ObservationContext) {
        for (symbol, bar) in &frame.bars_by_symbol {
            self.last.insert((*symbol).into(), Some(bar.close));
        }
        for symbol in &frame.missing_symbols { self.last.insert((*symbol).into(), None); }
    }
    fn on_trade(&mut self, event: &TradeEvent, _: &mut ObservationContext) {
        self.last.insert(event.symbol.into(), Some(event.price));
    }
    fn on_quote(&mut self, event: &QuoteEvent, _: &mut ObservationContext) {
        self.last.insert(event.symbol.into(), Some(event.mid()));
    }
}
impl Indicator for AuthoredIndicator {
    fn value(&self, symbol: &str) -> Option<Decimal> { self.last.get(symbol).copied().flatten() }
    fn sequence(&self) -> &IndicatorSequence { &self.sequence }
    fn sequence_mut(&mut self) -> &mut IndicatorSequence { &mut self.sequence }
}
"""
PROGRAM_TEMPLATES = {
    "study": STUDY_TEMPLATE,
    "strategy": STRATEGY_TEMPLATE,
    "indicator": INDICATOR_TEMPLATE,
}
