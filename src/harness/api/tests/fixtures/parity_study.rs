//! A minimal study program for the HTTP parity suite: counts observed events
//! and publishes the trade count and the mean trade price.
use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{
    export_study_program, MetricPublisher, QuoteEvent, StudyProgram, TradeEvent,
};

struct TradeObserver {
    trades: u64,
    quotes: u64,
    price_sum: f64,
}

impl StudyProgram for TradeObserver {
    fn new(_parameters: Value) -> Self {
        Self {
            trades: 0,
            quotes: 0,
            price_sum: 0.0,
        }
    }

    fn on_quote(&mut self, _event: &QuoteEvent) {
        self.quotes += 1;
    }

    fn on_trade(&mut self, event: &TradeEvent) {
        self.trades += 1;
        if let Ok(price) = event.price.to_string().parse::<f64>() {
            self.price_sum += price;
        }
    }

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("trade_observations", self.trades as f64);
        let _ = metrics.publish("quote_observations", self.quotes as f64);
        if self.trades > 0 {
            let _ = metrics.publish("global_sma", self.price_sum / self.trades as f64);
        }
    }
}

export_study_program!(TradeObserver);
