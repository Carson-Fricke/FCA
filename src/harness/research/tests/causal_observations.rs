//! Real authored replay seals prefix features/orders and resolves labels only in the host.
use std::collections::BTreeMap;

use backtest::backtest::{
    config::BacktestConfig,
    engine_core::{run_native_engine, EngineOptions},
    fixture::{
        ExchangeSession, MarketEvent, ReplayFingerprints, ReplayFixture, SyntheticDataOptions,
    },
    identity::build_default_manifest,
    outcome::BacktestOutcome,
    preflight::preflight_data,
};
use harness_strategy_jit::{JitStrategyRuntime, JitStudyRuntime, ManagedWorkspace};
use harness_trading::program::{StrategyInstance, StrategyRuntime};
use num::Decimal18;
use serde_json::{json, Value};

const MINUTE: i64 = 60_000_000_000;
const SOURCE: &str = include_str!("fixtures/causal_program.rs");

fn config(strategy: bool) -> BacktestConfig {
    let shell = if strategy { "backtest" } else { "study" };
    BacktestConfig::from_yaml(&format!(
        "shell: {shell}\njob_id: causal-replay\nstart: 2024-01-02T14:30:00Z\nend: 2024-01-02T14:40:00Z\ndata:\n  symbols: [SPY, AAA]\n  subscriptions:\n    - data_type: bars\n      symbols: [SPY, AAA]\n      interval: 1min\n"
    )).unwrap()
}

fn replay(
    workspace: &ManagedWorkspace,
    mode: &str,
    strategy: bool,
    future: i64,
    prefix: bool,
) -> Result<BacktestOutcome, String> {
    let config = config(strategy);
    let source = if strategy {
        SOURCE.replace("export_study_program", "export_strategy_program")
            .replace("ObservationContext", "OrderSink").replace("StudyProgram", "StrategyProgram")
            .replace("// STRATEGY_ACTION", "if self.frames == 1 { _context.place(harness_strategy_sdk::Order::market(\"SPY\", harness_strategy_sdk::Side::Buy, harness_strategy_sdk::Decimal::ONE)).unwrap(); }")
    } else {
        SOURCE.into()
    };
    let parameters = json!({"mode": mode});
    let runtime: Box<dyn StrategyRuntime> = if strategy {
        Box::new(JitStrategyRuntime::from_source(workspace, &source, &parameters).unwrap())
    } else {
        Box::new(JitStudyRuntime::from_source(workspace, &source, &parameters).unwrap())
    };
    // AAA is absent at the first horizon. Minute 2 is absent entirely, so a
    // SPY capture at minute 1 cannot use the price arriving at minute 3.
    let prices = if prefix {
        vec![(0, "SPY", 100), (0, "AAA", 200)]
    } else {
        vec![
            (0, "SPY", 100),
            (0, "AAA", 200),
            (1, "SPY", future),
            (3, "SPY", 110),
        ]
    };
    let events: Vec<_> = prices
        .into_iter()
        .map(|(minute, symbol, close)| MarketEvent::Bar {
            event_id: format!("{symbol}-{minute}"),
            timestamp_ns: config.start_ns + minute * MINUTE,
            sequence: 0,
            symbol: symbol.into(),
            open: Decimal18::from_i64(close),
            high: Decimal18::from_i64(close),
            low: Decimal18::from_i64(close),
            close: Decimal18::from_i64(close),
            volume: Decimal18::from_i64(100),
        })
        .collect();
    let fixture = ReplayFixture {
        stream: None,
        sessions: vec![ExchangeSession {
            session_date_days: config.start_ns.div_euclid(86_400_000_000_000),
            open_ns: config.start_ns,
            close_ns: config.end_ns,
        }],
        fingerprints: ReplayFingerprints::of_events(&events),
        events,
        source_id: "causal-test".into(),
        symbols: config.symbols.clone(),
        expected_data_fingerprint: None,
    };
    let preflight = preflight_data(&config, &fixture).unwrap();
    let manifest = build_default_manifest(&config, &fixture, &preflight).unwrap();
    run_native_engine(
        &config,
        &fixture,
        &preflight,
        manifest,
        &EngineOptions::default(),
        vec![StrategyInstance {
            instance_id: "causal".into(),
            runtime,
        }],
        &SyntheticDataOptions::default(),
    )
    .map_err(|error| error.to_string())
}

fn rows(outcome: &BacktestOutcome) -> Vec<BTreeMap<String, Value>> {
    let dataset = &outcome.exploratory_outputs["causal"]["causal/signal"]["dataset"];
    let columns = dataset["columns"].as_array().unwrap();
    dataset["rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            columns
                .iter()
                .zip(row.as_array().unwrap())
                .map(|(column, value)| (column.as_str().unwrap().into(), value.clone()))
                .collect()
        })
        .collect()
}

#[test]
fn real_replay_enforces_sealed_features_labels_and_publication_authority() {
    let directory = tempfile::tempdir().unwrap();
    let workspace = ManagedWorkspace::open(directory.path()).unwrap();
    for strategy in [false, true] {
        let original = replay(&workspace, "", strategy, 101, false).unwrap();
        assert!(original.completed());
        let changed = replay(&workspace, "", strategy, 999, false).unwrap();
        let prefix = replay(&workspace, "", strategy, 101, true).unwrap();
        let original_rows = rows(&original);
        let changed_rows = rows(&changed);
        let prefix_rows = rows(&prefix);
        assert_eq!(original_rows.len(), 4);
        for i in 0..2 {
            for column in [
                "observation_id",
                "symbol",
                "interval",
                "decision_at_ns",
                "outcome_due_at_ns",
                "reference_close",
                "feature/close",
            ] {
                assert_eq!(
                    original_rows[i][column], changed_rows[i][column],
                    "future perturbation changed {column}"
                );
                assert_eq!(
                    original_rows[i][column], prefix_rows[i][column],
                    "truncation changed {column}"
                );
            }
            assert_eq!(prefix_rows[i]["outcome_status"], "end_of_run");
            assert!(prefix_rows[i]["outcome_return_bps"].is_null());
        }
        assert_eq!(original_rows[0]["symbol"], "AAA");
        assert_eq!(original_rows[0]["outcome_status"], "missing");
        assert_eq!(original_rows[1]["outcome_status"], "observed");
        assert_eq!(original_rows[1]["outcome_return_bps"], 100.0);
        assert_ne!(
            original_rows[1]["outcome_return_bps"],
            changed_rows[1]["outcome_return_bps"]
        );
        assert_eq!(original_rows[2]["outcome_status"], "missing");
        assert!(original_rows[2]["outcome_close"].is_null());
        assert_eq!(original_rows[3]["outcome_status"], "end_of_run");
        assert!(original_rows
            .iter()
            .all(|row| row["feature/close"] != json!(999_999)));
        assert_eq!(
            original.exploratory_outputs["causal"]["retrospective"]["kind"],
            "table"
        );
        assert_eq!(original.orders.len(), usize::from(strategy));
        assert_eq!(original.orders.len(), prefix.orders.len());
        assert_eq!(original.orders.len(), changed.orders.len());
        if strategy {
            assert_eq!(original.orders[0].order_id, prefix.orders[0].order_id);
            assert_eq!(original.orders[0].order_id, changed.orders[0].order_id);
            assert_eq!(original.orders[0].quantity, prefix.orders[0].quantity);
            assert_eq!(original.orders[0].quantity, changed.orders[0].quantity);
            assert_eq!(original.orders[0].side, changed.orders[0].side);
        }
        for mode in [
            "metric_nonfinite",
            "invalid_table",
            "prefixed",
            "duplicate",
            "schema",
            "horizon",
            "overflow",
            "horizon_change",
            "absent",
            "nonfinite",
            "reserved",
            "forged",
        ] {
            let error = replay(&workspace, mode, strategy, 101, false).unwrap_err();
            if mode == "prefixed" {
                assert!(
                    error.contains("capture.dataset")
                        && error.contains("causal/signal")
                        && error.contains("unprefixed"),
                    "{error}"
                );
            }
            if mode == "nonfinite" {
                assert!(error.contains("features"), "{error}");
            }
            if mode == "horizon" {
                assert!(error.contains("horizon_ns"), "{error}");
            }
            if mode == "absent" {
                assert!(error.contains("capture.symbol"), "{error}");
            }

            if mode == "metric_nonfinite" {
                assert!(
                    error.contains("recon_roll_spread_bps") && error.contains("NaN"),
                    "{error}"
                );
                continue;
            }
            if mode == "invalid_table" {
                assert!(
                    error.contains("bad_rows") && error.contains("rows[0]"),
                    "{error}"
                );
                continue;
            }
            assert!(
                error.contains("capture") || error.contains("causal"),
                "{mode}: {error}"
            );
        }
    }
}
