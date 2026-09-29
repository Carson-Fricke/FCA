//! A minimal bar study program for the HTTP parity suite: counts observed
//! OHLCV bars and publishes the bar count and the mean bar close.
use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{export_study_program, BarEvent, MetricPublisher, StudyProgram};

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

    fn on_bar(&mut self, event: &BarEvent) {
        self.bars += 1;
        if let Ok(close) = event.close.to_string().parse::<f64>() {
            self.close_sum += close;
            self.last_close = close;
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

export_study_program!(BarObserver);
