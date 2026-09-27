//! Authored strategy fixture: submits one market buy on the first quote.
//!
//! This source is compiled by the JIT seam inside the test to a cdylib and
//! driven through the engine; it exercises the strategy surface — order
//! submission through the sink, fill observation, and metric publishing.

use harness_strategy_sdk::{
    export_strategy_program, Fill, MetricPublisher, OrderSink, OrderType, QuoteEvent, Side,
    StrategyProgram, SubmittedOrder, TimeInForce,
};
use harness_strategy_sdk::serde_json::Value;

#[derive(Debug, Default)]
struct BuyOnce {
    submitted: bool,
    filled: bool,
}

impl StrategyProgram for BuyOnce {
    fn new(_parameters: Value) -> Self {
        Self::default()
    }

    fn on_quote(&mut self, event: &QuoteEvent, orders: &mut OrderSink) {
        if self.submitted {
            return;
        }
        self.submitted = true;
        let _ = orders.submit(SubmittedOrder {
            order_id: "jit-buy".to_string(),
            symbol: event.symbol.to_string(),
            side: Side::Buy,
            quantity: "10".parse().unwrap_or_default(),
            order_type: OrderType::Market,
            time_in_force: TimeInForce::Day,
            limit_price: None,
            stop_price: None,
            sequence: event.sequence,
            event_id: format!("jit-order-{}", event.sequence),
        });
    }

    fn on_fill(&mut self, fill: &Fill) {
        if fill.order_id == "jit-buy" {
            self.filled = true;
        }
    }

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("jit_submitted", i32::from(self.submitted) as f64);
        let _ = metrics.publish("jit_filled", i32::from(self.filled) as f64);
    }
}

export_strategy_program!(BuyOnce);
