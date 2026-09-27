//! Authored study fixture: counts events and publishes the last quote mid.
//!
//! This source is compiled by the JIT seam inside the test to a cdylib and
//! driven through the engine; it exercises the full study surface — quote and
//! trade callbacks, exact-decimal reads, and metric publishing.

use harness_strategy_sdk::{
    export_study_program, Decimal, MetricPublisher, QuoteEvent, StudyProgram, TradeEvent,
};
use harness_strategy_sdk::serde_json::Value;

#[derive(Debug, Default)]
struct CountStudy {
    quotes: u64,
    trades: u64,
    last_mid: Option<Decimal>,
}

impl StudyProgram for CountStudy {
    fn new(_parameters: Value) -> Self {
        Self::default()
    }

    fn on_quote(&mut self, event: &QuoteEvent) {
        self.quotes += 1;
        self.last_mid = Some(event.mid());
    }

    fn on_trade(&mut self, _event: &TradeEvent) {
        self.trades += 1;
    }

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("study_quote_count", self.quotes as f64);
        let _ = metrics.publish("study_trade_count", self.trades as f64);
        if let Some(mid) = self.last_mid {
            let _ = metrics.publish("study_last_mid", mid.to_string().parse().unwrap_or(0.0));
        }
    }
}

export_study_program!(CountStudy);
