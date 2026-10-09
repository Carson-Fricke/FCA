//! Strategy fixture with a program-owned trade indicator and atomic bar orders.
use harness_strategy_sdk::{
    export_strategy_program, BarFrame, Decimal, Indicator, IndicatorSequence, MarketConsumer,
    MetricPublisher, ObservationContext, OrderSink, OrderType, ProgramStart, QuoteEvent, Side,
    StrategyProgram, SubmittedOrder, Subscription, TimeInForce, TradeEvent,
};

struct TradeCount {
    sequence: IndicatorSequence,
    count: u64,
    mutate: bool,
}
impl MarketConsumer for TradeCount {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Trade {
            symbols: if self.mutate && self.count > 0 {
                vec!["DEP".into(), "BBB".into()]
            } else {
                vec!["DEP".into()]
            },
        }]
    }
    fn on_trade(&mut self, _: &TradeEvent, _: &mut ObservationContext) {
        self.count += 1;
    }
}
impl Indicator for TradeCount {
    fn sequence(&self) -> &IndicatorSequence {
        &self.sequence
    }
    fn sequence_mut(&mut self) -> &mut IndicatorSequence {
        &mut self.sequence
    }
    fn value(&self, _: &str) -> Option<Decimal> {
        Some(Decimal::from_i128(self.count as i128))
    }
}

struct MixedStrategy {
    indicator: TradeCount,
    alternate: TradeCount,
    quotes: u64,
    trades: u64,
    frames: u64,
    invalid: bool,
    mutation: String,
    changed: bool,
    detached: bool,
}
impl StrategyProgram for MixedStrategy {
    fn new(parameters: harness_strategy_sdk::serde_json::Value) -> Self {
        Self {
            indicator: TradeCount {
                sequence: IndicatorSequence::ticks(60_000_000_000).unwrap(),
                count: 0,
                mutate: parameters["mutation"] == "dependency",
            },
            alternate: TradeCount {
                sequence: IndicatorSequence::ticks(60_000_000_000).unwrap(),
                count: 0,
                mutate: false,
            },
            quotes: 0,
            trades: 0,
            frames: 0,
            invalid: parameters["invalid"].as_bool().unwrap_or(false),
            mutation: parameters["mutation"].as_str().unwrap_or("").into(),
            changed: false,
            detached: false,
        }
    }
    fn on_start(&mut self, _: &ProgramStart) {
        if self.mutation == "start" {
            self.changed = true;
        }
    }
    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        assert!(self.indicator.sequence().completed().is_empty());
        let partial = &self.indicator.sequence().in_progress()["DEP"];
        assert_eq!(partial.close, Some(Decimal::from_i128(1)));
        assert_eq!(partial.available_at_ns, None);
        assert_eq!(partial.observations, 1);
        metrics.publish("quotes", self.quotes as f64).unwrap();
        metrics.publish("trades", self.trades as f64).unwrap();
        metrics.publish("frames", self.frames as f64).unwrap();
        metrics
            .publish("dependency", self.indicator.count as f64)
            .unwrap();
    }
}
impl MarketConsumer for MixedStrategy {
    type Context = OrderSink;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![
            Subscription::Quote {
                symbols: vec!["AAA".into()],
            },
            Subscription::Trade {
                symbols: if self.changed {
                    vec!["BBB".into(), "DEP".into()]
                } else {
                    vec!["BBB".into()]
                },
            },
            Subscription::Bars {
                symbols: vec!["AAA".into()],
                interval: if self.invalid { "invalid" } else { "1min" }.into(),
            },
        ]
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        if self.detached {
            vec![&mut self.alternate]
        } else {
            vec![&mut self.indicator]
        }
    }
    fn on_quote(&mut self, _: &QuoteEvent, _: &mut OrderSink) {
        self.quotes += 1;
        if self.mutation == "callback" {
            self.changed = true;
        }
        if self.mutation == "topology" {
            self.detached = true;
        }
    }
    fn on_trade(&mut self, _: &TradeEvent, _: &mut OrderSink) {
        self.trades += 1;
    }
    fn on_bar(&mut self, frame: &BarFrame, orders: &mut OrderSink) {
        assert_eq!(frame.bars_by_symbol.len(), 1);
        assert!(frame.bars_by_symbol.contains_key("AAA"));
        assert_eq!(self.indicator.value("DEP").unwrap(), Decimal::from_i128(1));
        self.frames += 1;
        orders
            .submit(SubmittedOrder {
                order_id: "frame-order".into(),
                symbol: "AAA".into(),
                side: Side::Buy,
                quantity: Decimal::from_i128(1),
                order_type: OrderType::Market,
                time_in_force: TimeInForce::Day,
                limit_price: None,
                stop_price: None,
                sequence: 1,
                event_id: "frame-intent".into(),
            })
            .unwrap();
    }
}
export_strategy_program!(MixedStrategy);
