//! A minimal buy-and-hold authoring example: submit one market buy on the
//! first quote and hold it through the replay.
use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{
    export_strategy_program, Fill, MetricPublisher, OrderSink, OrderType, QuoteEvent, Side,
    StrategyProgram, SubmittedOrder, TimeInForce,
};

struct BuyAndHold {
    submitted: bool,
    filled: bool,
}

impl StrategyProgram for BuyAndHold {
    fn new(_parameters: Value) -> Self {
        Self {
            submitted: false,
            filled: false,
        }
    }

    fn on_quote(&mut self, event: &QuoteEvent, orders: &mut OrderSink) {
        if self.submitted {
            return;
        }
        self.submitted = true;
        let _ = orders.submit(SubmittedOrder {
            order_id: format!("bah-buy-{}", event.sequence),
            symbol: event.symbol.to_string(),
            side: Side::Buy,
            quantity: "10".parse().unwrap_or_default(),
            order_type: OrderType::Market,
            time_in_force: TimeInForce::Day,
            limit_price: None,
            stop_price: None,
            sequence: event.sequence,
            event_id: format!("bah-buy-event-{}", event.sequence),
        });
    }

    fn on_fill(&mut self, fill: &Fill) {
        if fill.side == "buy" {
            self.filled = true;
        }
    }

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("bah_filled", f64::from(self.filled));
    }
}

export_strategy_program!(BuyAndHold);
