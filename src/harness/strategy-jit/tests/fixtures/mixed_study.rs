//! Mixed study with a nested indicator dependency and observation-only context.
use harness_strategy_sdk::{
    export_study_program, BarFrame, Decimal, Indicator, IndicatorSequence, MarketConsumer,
    MetricPublisher, ObservationContext, QuoteEvent, StudyProgram, Subscription, TradeEvent,
};

struct QuoteCount {
    sequence: IndicatorSequence,
    count: u64,
}
impl MarketConsumer for QuoteCount {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Quote {
            symbols: vec!["DEP".into()],
        }]
    }
    fn on_quote(&mut self, _: &QuoteEvent, _: &mut ObservationContext) {
        self.count += 1;
    }
}
impl Indicator for QuoteCount {
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

struct Nested {
    sequence: IndicatorSequence,
    child: QuoteCount,
}
impl MarketConsumer for Nested {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![]
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        vec![&mut self.child]
    }
}
impl Indicator for Nested {
    fn sequence(&self) -> &IndicatorSequence {
        &self.sequence
    }
    fn sequence_mut(&mut self) -> &mut IndicatorSequence {
        &mut self.sequence
    }
    fn value(&self, _: &str) -> Option<Decimal> {
        self.child.value("DEP")
    }
}

struct MixedStudy {
    indicator: Nested,
    quotes: u64,
    trades: u64,
    frames: u64,
}
impl StudyProgram for MixedStudy {
    fn new(_: harness_strategy_sdk::serde_json::Value) -> Self {
        Self {
            indicator: Nested {
                sequence: IndicatorSequence::ticks(60_000_000_000).unwrap(),
                child: QuoteCount {
                    count: 0,
                    sequence: IndicatorSequence::ticks(60_000_000_000).unwrap(),
                },
            },
            quotes: 0,
            trades: 0,
            frames: 0,
        }
    }
    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        if let Some(partial) = self.indicator.sequence().in_progress().get("DEP") {
            assert_eq!(partial.close, Some(Decimal::from_i128(1)));
            assert_eq!(partial.available_at_ns, None);
            assert_eq!(partial.observations, 1);
        } else {
            let completed = self.indicator.sequence().completed();
            let bars = &completed[&("DEP".into(), "ticks:60000000000".into())];
            assert_eq!(bars[0].close, Some(Decimal::from_i128(1)));
            assert!(bars[0].available_at_ns.is_some());
        }
        metrics.publish_output("signal", &harness_strategy_sdk::StudyOutput::Series {
            points: vec![harness_strategy_sdk::OutputPoint { symbol: "AAA".into(), interval: "1min".into(), event_time_ns: 20, available_at_ns: 25, value: Some(1.0), undefined_reason: None }],
        }).unwrap();
        metrics.publish("quotes", self.quotes as f64).unwrap();
        metrics.publish("trades", self.trades as f64).unwrap();
        metrics.publish("frames", self.frames as f64).unwrap();
        metrics
            .publish("dependency", self.indicator.child.count as f64)
            .unwrap();
    }
}
impl MarketConsumer for MixedStudy {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![
            Subscription::Quote {
                symbols: vec!["AAA".into()],
            },
            Subscription::Trade {
                symbols: vec!["BBB".into()],
            },
            Subscription::Bars {
                symbols: vec!["AAA".into()],
                interval: "1min".into(),
            },
            Subscription::Bars { symbols: vec!["AAA".into()], interval: "5min".into() },
            Subscription::Bars { symbols: vec!["AAA".into()], interval: "1d".into() },
        ]
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        vec![&mut self.indicator]
    }
    fn on_quote(&mut self, _: &QuoteEvent, _: &mut ObservationContext) {
        self.quotes += 1;
    }
    fn on_trade(&mut self, _: &TradeEvent, _: &mut ObservationContext) {
        self.trades += 1;
    }
    fn on_bar(&mut self, frame: &BarFrame, _: &mut ObservationContext) {
        assert_eq!(frame.bars_by_symbol.len(), 1);
        assert_eq!(self.indicator.value("DEP").unwrap(), Decimal::from_i128(1));
        self.frames += 1;
    }
}
export_study_program!(MixedStudy);
