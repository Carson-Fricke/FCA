//! A minimal buy-and-hold authoring example: submit one market buy on the
//! first quote and hold it through the replay.
use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{
    export_strategy_program, Fill, MarketConsumer, MetricPublisher, OrderSink, OrderType,
    QuoteEvent, Side, StrategyProgram, SubmittedOrder, Subscription, TimeInForce,
};

struct BuyAndHold {
    symbols: Vec<String>,
    submitted: bool,
    filled: bool,
}

impl StrategyProgram for BuyAndHold {
    fn new(parameters: Value) -> Self {
        Self {
            symbols: parameters
                .get("symbols")
                .and_then(|v| harness_strategy_sdk::serde_json::from_value(v.clone()).ok())
                .unwrap_or_else(|| vec!["SPY".into()]),
            submitted: false,
            filled: false,
        }
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

impl MarketConsumer for BuyAndHold {
    type Context = OrderSink;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Quote {
            symbols: self.symbols.clone(),
        }]
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
}

export_strategy_program!(BuyAndHold);
