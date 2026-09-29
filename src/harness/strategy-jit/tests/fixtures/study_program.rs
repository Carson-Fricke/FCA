//! Authored study fixture: folds OHLCV bars and publishes exact bar reads.
//!
//! This source is compiled by the JIT seam inside the test to a cdylib and
//! driven through the explicit bar host entry; it exercises the full study
//! surface — the bar callback, exact-decimal reads, and metric publishing.
//! The optional `expected` parameters array pins each bar's exact decimal
//! strings, so the host can assert a scale-preserving crossing.

use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{
    export_study_program, BarEvent, Decimal, MetricPublisher, StudyProgram,
};

#[derive(Debug, Default)]
struct BarStudy {
    bars: u64,
    mismatches: u64,
    last_open: Option<Decimal>,
    last_high: Option<Decimal>,
    last_low: Option<Decimal>,
    last_close: Option<Decimal>,
    last_volume: Option<Decimal>,
    expected: Vec<ExpectedBar>,
}

#[derive(Debug)]
struct ExpectedBar {
    open: String,
    high: String,
    low: String,
    close: String,
    volume: String,
}

impl BarStudy {
    fn check(value: Decimal, expected: &str, mismatches: &mut u64) {
        if value.to_string() != expected {
            *mismatches += 1;
        }
    }
}

impl StudyProgram for BarStudy {
    fn new(parameters: Value) -> Self {
        let expected = parameters
            .get("expected")
            .and_then(Value::as_array)
            .map(|bars| {
                bars.iter()
                    .filter_map(|bar| {
                        Some(ExpectedBar {
                            open: bar.get("open")?.as_str()?.to_string(),
                            high: bar.get("high")?.as_str()?.to_string(),
                            low: bar.get("low")?.as_str()?.to_string(),
                            close: bar.get("close")?.as_str()?.to_string(),
                            volume: bar.get("volume")?.as_str()?.to_string(),
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();
        Self {
            expected,
            ..Self::default()
        }
    }

    fn on_bar(&mut self, event: &BarEvent) {
        if let Some(expected) = self.expected.get(self.bars as usize) {
            let mut mismatches = 0;
            Self::check(event.open, &expected.open, &mut mismatches);
            Self::check(event.high, &expected.high, &mut mismatches);
            Self::check(event.low, &expected.low, &mut mismatches);
            Self::check(event.close, &expected.close, &mut mismatches);
            Self::check(event.volume, &expected.volume, &mut mismatches);
            self.mismatches += mismatches;
        }
        self.bars += 1;
        self.last_open = Some(event.open);
        self.last_high = Some(event.high);
        self.last_low = Some(event.low);
        self.last_close = Some(event.close);
        self.last_volume = Some(event.volume);
    }

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("study_bar_count", self.bars as f64);
        let _ = metrics.publish("study_bar_mismatches", self.mismatches as f64);
        publish_decimal(metrics, "study_last_open", self.last_open);
        publish_decimal(metrics, "study_last_high", self.last_high);
        publish_decimal(metrics, "study_last_low", self.last_low);
        publish_decimal(metrics, "study_last_close", self.last_close);
        publish_decimal(metrics, "study_last_volume", self.last_volume);
    }
}

fn publish_decimal(metrics: &mut MetricPublisher, name: &str, value: Option<Decimal>) {
    if let Some(value) = value {
        let _ = metrics.publish(name, value.to_string().parse().unwrap_or(0.0));
    }
}

export_study_program!(BarStudy);
