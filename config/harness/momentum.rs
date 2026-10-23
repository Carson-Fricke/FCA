//! A minimal momentum authoring example: enter after `lookback`
//! consecutively rising quote midpoints, liquidate once the entry has
//! filled, and publish the fill count. This is the committed benchmark
//! program that replaced the Python intraday momentum example.
use std::cmp::Ordering;
use std::collections::BTreeMap;

use harness_strategy_sdk::building_blocks::{FillLedger, PendingOrder};
use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{
    export_strategy_program, Decimal, Fill, MarketConsumer, MetricPublisher, OrderSink, OrderType,
    QuoteEvent, Side, StrategyProgram, SubmittedOrder, Subscription, TimeInForce,
};

struct Momentum {
    symbols: Vec<String>,
    lookback: u64,
    quantity: String,
    streak: u64,
    last_mid: Option<Decimal>,
    entered: bool,
    filled: bool,
    liquidated: bool,
    fills: u64,
    ledgers: BTreeMap<String, FillLedger>,
    entry: Option<PendingOrder>,
    exit: Option<PendingOrder>,
}

impl StrategyProgram for Momentum {
    fn new(parameters: Value) -> Self {
        let lookback = parameters
            .get("lookback")
            .and_then(Value::as_u64)
            .unwrap_or(4)
            .max(1);
        let quantity = match parameters.get("quantity") {
            Some(Value::String(text)) => text.clone(),
            Some(Value::Number(number)) => number.to_string(),
            _ => "10".to_string(),
        };
        Self {
            symbols: parameters
                .get("symbols")
                .and_then(|v| harness_strategy_sdk::serde_json::from_value(v.clone()).ok())
                .unwrap_or_else(|| vec!["SPY".into()]),
            lookback,
            quantity,
            streak: 0,
            last_mid: None,
            entered: false,
            filled: false,
            liquidated: false,
            fills: 0,
            ledgers: BTreeMap::new(),
            entry: None,
            exit: None,
        }
    }

    fn on_fill(&mut self, fill: &Fill) {
        let ledger = self.ledgers.entry(fill.symbol.into()).or_insert_with(|| FillLedger::new(fill.symbol));
        if ledger.apply(fill).expect("valid booked fill") { self.fills += 1; }
        self.filled = self.entry.as_ref().is_some_and(|order| order.is_filled(ledger));
        self.liquidated = ledger.quantity().is_zero()
            && self.exit.as_ref().is_some_and(|order| order.is_filled(ledger));
    }

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("mom_fills", self.fills as f64);
        let quantity: f64 = self.ledgers.values().map(|ledger| ledger.quantity().abs().to_f64()).sum();
        let _ = metrics.publish("mom_open_quantity", quantity);
    }
}

impl MarketConsumer for Momentum {
    type Context = OrderSink;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Quote {
            symbols: self.symbols.clone(),
        }]
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
        if !self.entered && self.streak >= self.lookback {
            let order = self.buy(event);
            if let Ok(pending) = PendingOrder::new(&order.order_id, order.quantity) {
                if orders.submit(order).is_ok() { self.entry = Some(pending); self.entered = true; }
            }
        } else if self.entered && self.filled && !self.liquidated && self.exit.is_none() {
            let order = self.sell(event);
            if let Ok(pending) = PendingOrder::new(&order.order_id, order.quantity) {
                if orders.submit(order).is_ok() { self.exit = Some(pending); }
            }
        }
    }
}

impl Momentum {
    fn buy(&self, event: &QuoteEvent) -> SubmittedOrder {
        SubmittedOrder {
            order_id: format!("mom-buy-{}", event.sequence),
            symbol: event.symbol.to_string(),
            side: Side::Buy,
            quantity: self.quantity.parse().unwrap_or_default(),
            order_type: OrderType::Market,
            time_in_force: TimeInForce::Day,
            limit_price: None,
            stop_price: None,
            sequence: event.sequence,
            event_id: format!("mom-buy-event-{}", event.sequence),
        }
    }

    fn sell(&self, event: &QuoteEvent) -> SubmittedOrder {
        SubmittedOrder {
            order_id: format!("mom-sell-{}", event.sequence),
            symbol: event.symbol.to_string(),
            side: Side::Sell,
            quantity: self.ledgers.get(event.symbol).map_or(Decimal::ZERO, |ledger| ledger.quantity().abs()),
            order_type: OrderType::Market,
            time_in_force: TimeInForce::Day,
            limit_price: None,
            stop_price: None,
            sequence: event.sequence,
            event_id: format!("mom-sell-event-{}", event.sequence),
        }
    }
}

export_strategy_program!(Momentum);
