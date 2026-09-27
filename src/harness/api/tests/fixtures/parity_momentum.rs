//! A minimal momentum program for the HTTP parity suite: enter after four
//! consecutively rising quote midpoints, liquidate once the entry has filled.
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
        Self {
            streak: 0,
            last_mid: None,
            entered: false,
            filled: false,
            liquidated: false,
        }
    }

    fn on_quote(&mut self, event: &QuoteEvent, orders: &mut OrderSink) {
        let mid = event.mid();
        if let Some(previous) = self.last_mid {
            self.streak = if mid.cmp_value(previous) == Ordering::Greater {
                self.streak + 1
            } else {
                0
            };
        }
        self.last_mid = Some(mid);
        if !self.entered && self.streak >= 4 {
            self.entered = true;
            let _ = orders.submit(SubmittedOrder {
                order_id: format!("parity-buy-{}", event.sequence),
                symbol: event.symbol.to_string(),
                side: Side::Buy,
                quantity: "10".parse().unwrap_or_default(),
                order_type: OrderType::Market,
                time_in_force: TimeInForce::Day,
                limit_price: None,
                stop_price: None,
                sequence: event.sequence,
                event_id: format!("parity-buy-event-{}", event.sequence),
            });
        } else if self.entered && self.filled && !self.liquidated {
            self.liquidated = true;
            let _ = orders.submit(SubmittedOrder {
                order_id: format!("parity-sell-{}", event.sequence),
                symbol: event.symbol.to_string(),
                side: Side::Sell,
                quantity: "10".parse().unwrap_or_default(),
                order_type: OrderType::Market,
                time_in_force: TimeInForce::Day,
                limit_price: None,
                stop_price: None,
                sequence: event.sequence,
                event_id: format!("parity-sell-event-{}", event.sequence),
            });
        }
    }

    fn on_fill(&mut self, _fill: &Fill) {
        self.filled = true;
    }

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("parity_fills", f64::from(self.liquidated));
    }
}

export_strategy_program!(Momentum);
