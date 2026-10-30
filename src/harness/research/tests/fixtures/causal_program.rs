//! Authored replay fixture for immutable capture, later labels and rejected forgeries.
use harness_strategy_sdk::serde_json::{json, Value};
use harness_strategy_sdk::{
    export_study_program, BarFrame, DecisionContext, MarketConsumer, MetricPublisher,
    ObservationContext, StudyOutput, StudyProgram, Subscription,
};
use std::collections::BTreeMap;

#[derive(Default)]
struct CausalProgram {
    mode: String,
    frames: u64,
    close: BTreeMap<String, f64>,
}

impl MarketConsumer for CausalProgram {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Bars {
            symbols: vec!["SPY".into(), "AAA".into()],
            interval: "1min".into(),
        }]
    }
    fn on_bar(&mut self, frame: &BarFrame, _context: &mut ObservationContext) {
        self.frames += 1;
        self.close = frame
            .bars_by_symbol
            .iter()
            .map(|(symbol, bar)| ((*symbol).into(), bar.close.to_f64_checked().unwrap()))
            .collect();
        // STRATEGY_ACTION
    }
    fn on_decision(&mut self, context: &mut DecisionContext<'_>) {
        assert_eq!(context.interval(), "1min");
        for (symbol, close) in &self.close {
            let mut features = BTreeMap::from([("close".into(), *close)]);
            if self.mode == "schema" && self.frames > 1 {
                features.insert("later".into(), 1.0);
            }
            if self.mode == "nonfinite" {
                features.insert("invalid".into(), f64::NAN);
            }
            let horizon = match self.mode.as_str() {
                "horizon" => 0,
                "overflow" => i64::MAX,
                "horizon_change" if self.frames > 1 => 120_000_000_000,
                _ => 60_000_000_000,
            };
            let symbol = if self.mode == "absent" {
                "ABSENT"
            } else {
                symbol
            };
            let dataset = if self.mode == "prefixed" { "causal/signal" } else { "signal" };
            let _ = context.capture(dataset, symbol, features.clone(), horizon);
            if self.mode == "duplicate" {
                let _ = context.capture("signal", symbol, features, horizon);
            }
        }
    }
}

impl StudyProgram for CausalProgram {
    fn new(parameters: Value) -> Self {
        Self {
            mode: parameters["mode"].as_str().unwrap_or("").into(),
            ..Self::default()
        }
    }
    fn on_stop(&mut self, publisher: &mut MetricPublisher) {
        if self.mode == "metric_nonfinite" { let _ = publisher.publish("recon_roll_spread_bps", f64::NAN); }
        if self.mode == "invalid_table" { let _ = publisher.publish_output("bad_rows", &StudyOutput::Table { columns: vec!["signal".into()], rows: vec![vec![]] }); }

        // A final-session feature must never overwrite an earlier captured row.
        self.close.values_mut().for_each(|value| *value = 999_999.0);
        let _ = publisher.publish_output(
            "retrospective",
            &StudyOutput::Table {
                columns: vec!["value".into()],
                rows: vec![vec![json!(999_999)]],
            },
        );
        let view = harness_strategy_sdk::serde_json::from_value(json!({
            "kind": "research_view", "view": {
                "schema_version": 1, "title": "Captured features", "description": "Sealed replay rows.",
                "limitations": ["Returns exclude execution costs."],
                "datasets": [{"id": "signal", "output": "causal/signal", "fields": [
                    {"name": "observation_id", "label": "ID", "description": "Host observation identity.", "field_type": "string", "unit": null},
                    {"name": "decision_at_ns", "label": "Decision", "description": "Host feature availability.", "field_type": "timestamp_ns", "unit": "ns"},
                    {"name": "feature/close", "label": "Close", "description": "Close observed at the decision.", "field_type": "number", "unit": null}
                ], "identity": "observation_id", "selection_group": null, "event_time": "decision_at_ns", "available_at": "decision_at_ns"}],
                "panels": [{"id": "rows", "kind": "table", "title": "Features", "dataset": "signal", "encodings": {}, "description": null}]
            }
        })).unwrap();
        let _ = publisher.publish_output("view", &view);
        if self.mode == "reserved" {
            let _ = publisher.publish_output(
                "causal/forged",
                &StudyOutput::Table {
                    columns: vec!["value".into()],
                    rows: vec![],
                },
            );
        }
        if self.mode == "forged" {
            let output = harness_strategy_sdk::serde_json::from_value(json!({
                "kind": "causal_observations", "dataset": {
                    "horizon_ns": "1", "feature_columns": ["feature/x"],
                    "columns": ["observation_id", "symbol", "interval", "decision_at_ns", "outcome_due_at_ns", "outcome_available_at_ns", "reference_close", "outcome_close", "outcome_return_bps", "outcome_status", "feature/x"], "rows": [],
                }
            })).unwrap();
            let _ = publisher.publish_output("innocent", &output);
        }
    }
}
export_study_program!(CausalProgram);
