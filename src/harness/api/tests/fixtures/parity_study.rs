//! A minimal bar study program for the HTTP parity suite: counts observed
//! OHLCV bars and publishes the bar count and the mean bar close.
use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{
    export_study_program, BarFrame, MarketConsumer, MetricPublisher, ObservationContext,
    StudyProgram, Subscription,
};

struct BarObserver {
    bars: u64,
    close_sum: f64,
    last_close: f64,
}

impl StudyProgram for BarObserver {
    fn new(_parameters: Value) -> Self {
        Self {
            bars: 0,
            close_sum: 0.0,
            last_close: 0.0,
        }
    }

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("bar_observations", self.bars as f64);
        if self.bars > 0 {
            let _ = metrics.publish("mean_close", self.close_sum / self.bars as f64);
            let _ = metrics.publish("last_close", self.last_close);
        }
    }
}

impl MarketConsumer for BarObserver {
    type Context = ObservationContext;

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Bars {
            symbols: vec!["SPY".into()],
            interval: "1min".into(),
        }]
    }

    fn on_bar(&mut self, frame: &BarFrame, _: &mut ObservationContext) {
        for event in frame.bars_by_symbol.values() {
            self.bars += 1;
            let close = event.close.to_f64();
            self.close_sum += close;
            self.last_close = close;
        }
    }
}

export_study_program!(BarObserver);
