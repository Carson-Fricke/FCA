//! Configuration-level screening runs: context building, JIT execution, the
//! ranked rows, and the exact `screen.json` artifact (complete and failed).
//!
//! Every test injects a deterministic fake [`ScreeningDataSource`] and a
//! private managed workspace, so no network, lake files, or shared JIT state
//! are touched.

use std::collections::BTreeMap;
use std::path::PathBuf;

use backtest::backtest::screen::ScreenConfig;
use harness_strategy_jit::{
    run_screening_configuration, ScreenRunOptions, ScreenStatus, ScreeningDataSource,
    ScreeningDataSourceError, SCREEN_ARTIFACT, SCREEN_SCHEMA,
};
use harness_strategy_sdk::serde_json::{json, Value};
use harness_strategy_sdk::Bar;

const SCREENING_SOURCE: &str = include_str!("fixtures/screening_program.rs");

fn temp_dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-screen-run-{}-{name}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

fn bar(date: &str, close: &str) -> Bar {
    Bar {
        date: date.to_string(),
        open: "100.00".to_string(),
        high: "101.50".to_string(),
        low: "99.75".to_string(),
        close: close.to_string(),
        volume: "123456".to_string(),
    }
}

fn facts(entries: &[(&str, &str)]) -> BTreeMap<String, String> {
    entries
        .iter()
        .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
        .collect()
}

/// The fixture universe, deliberately unsorted so the host-side
/// canonicalization is exercised.
struct FakeSource {
    fail: bool,
}

impl ScreeningDataSource for FakeSource {
    fn universe(&self, as_of: &str) -> Result<Vec<String>, ScreeningDataSourceError> {
        assert_eq!(as_of, "2024-01-02");
        if self.fail {
            return Err(ScreeningDataSourceError::data(
                "no bar coverage for 2024-01-02",
            ));
        }
        Ok(["EEE", "CCC", "BBB", "AAA", "DDD"]
            .iter()
            .map(|symbol| (*symbol).to_string())
            .collect())
    }

    fn bars(
        &self,
        symbols: &[String],
        interval: &str,
        as_of: &str,
        lookback_days: u32,
    ) -> Result<BTreeMap<String, Vec<Bar>>, ScreeningDataSourceError> {
        assert_eq!(
            symbols,
            ["AAA", "BBB", "CCC", "DDD", "EEE"]
                .iter()
                .map(|symbol| (*symbol).to_string())
                .collect::<Vec<_>>()
        );
        assert_eq!(interval, "1d");
        assert_eq!(as_of, "2024-01-02");
        assert_eq!(lookback_days, 3);
        Ok(BTreeMap::from([
            ("AAA".to_string(), vec![bar("2024-01-02", "101.00")]),
            ("BBB".to_string(), vec![bar("2024-01-02", "99.50")]),
            ("CCC".to_string(), vec![bar("2024-01-02", "102.25")]),
            ("DDD".to_string(), vec![bar("2024-01-02", "100.00")]),
        ]))
    }

    fn facts(
        &self,
        symbols: &[String],
        as_of: &str,
    ) -> Result<BTreeMap<String, BTreeMap<String, String>>, ScreeningDataSourceError> {
        assert_eq!(symbols.len(), 5);
        assert_eq!(as_of, "2024-01-02");
        Ok(BTreeMap::from([
            ("AAA".to_string(), facts(&[("momentum", "9.5")])),
            ("BBB".to_string(), facts(&[("momentum", "10")])),
            ("CCC".to_string(), facts(&[("momentum", "9.50")])),
            ("DDD".to_string(), facts(&[("momentum", "-1")])),
            ("EEE".to_string(), facts(&[("sector", "utilities")])),
        ]))
    }
}

fn config(source: &str, parameters: Value) -> ScreenConfig {
    let digest = backtest::sha256::digest_hex(source.as_bytes());
    let document = json!({
        "schema_version": 1,
        "shell": "screen",
        "name": "fixture-screen",
        "as_of": "2024-01-02",
        "bar_interval": "1d",
        "lookback_days": 3,
        "rank_metric": "momentum",
        "universe_revision": "test-2024-01",
        "source": source,
        "source_sha256": digest,
        "bundle_hash": digest,
        "definition_id": format!("sha256:{}", "a".repeat(64)),
        "definition_hash": "b".repeat(64),
        "parameters": parameters,
    });
    ScreenConfig::from_json_value(&document).expect("valid fixture config")
}

#[test]
fn writes_the_exact_ranked_screen_artifact() {
    let workspace = temp_dir("workspace");
    let output = temp_dir("output");
    let configuration = config(SCREENING_SOURCE, json!({"label": "fixture"}));
    let run_id = configuration.run_id();

    let (outcome, artifact_path) = run_screening_configuration(
        &configuration,
        &FakeSource { fail: false },
        &ScreenRunOptions {
            workspace_root: Some(workspace.clone()),
        },
        Some(&output),
    )
    .expect("run");

    assert_eq!(outcome.status, ScreenStatus::Complete);
    assert!(outcome.completed());
    assert_eq!(outcome.row_count(), 5);
    assert_eq!(artifact_path, output.join(SCREEN_ARTIFACT));
    let symbols: Vec<&str> = outcome.rows.iter().map(|row| row.symbol.as_str()).collect();
    // Primary metric descending with exact decimals; `9.5` ties `9.50` and the
    // symbol tiebreak orders them; the row missing the metric sorts last.
    assert_eq!(symbols, ["BBB", "AAA", "CCC", "DDD", "EEE"]);

    let text = std::fs::read_to_string(&artifact_path).expect("artifact");
    assert!(text.ends_with('\n'));
    let document: Value = serde_json::from_str(&text).expect("artifact json");

    let expected_rows = json!([
        {"symbol": "BBB", "metrics": {"close": "99.50", "label": "fixture", "momentum": "10"}},
        {"symbol": "AAA", "metrics": {"close": "101.00", "label": "fixture", "momentum": "9.5"}},
        {"symbol": "CCC", "metrics": {"close": "102.25", "label": "fixture", "momentum": "9.50"}},
        {"symbol": "DDD", "metrics": {"close": "100.00", "label": "fixture", "momentum": "-1"}},
        {"symbol": "EEE", "metrics": {"label": "fixture"}}
    ]);
    let provenance = &document["provenance"];
    assert_eq!(
        provenance["source_digest"].as_str(),
        Some(configuration.bundle_hash.as_str())
    );
    assert_eq!(
        provenance["toolchain_digest"].as_str().map(str::len),
        Some(64)
    );
    assert_eq!(provenance["run_digest"].as_str().map(str::len), Some(64));

    let expected = json!({
        "schema": SCREEN_SCHEMA,
        "status": "complete",
        "run_id": run_id,
        "as_of": "2024-01-02",
        "definition_id": configuration.definition_id,
        "definition_hash": configuration.definition_hash,
        "bundle_hash": configuration.bundle_hash,
        "rank_metric": "momentum",
        "bar_interval": "1d",
        "data_window": {"start": "2023-12-30", "end": "2024-01-02"},
        "universe_revision": "test-2024-01",
        "rows": expected_rows,
        "provenance": provenance,
    });
    assert_eq!(document, expected);

    let mut keys: Vec<&str> = document
        .as_object()
        .expect("object")
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    let mut expected_keys = vec![
        "schema",
        "status",
        "run_id",
        "as_of",
        "definition_id",
        "definition_hash",
        "bundle_hash",
        "rank_metric",
        "bar_interval",
        "data_window",
        "universe_revision",
        "rows",
        "provenance",
    ];
    expected_keys.sort_unstable();
    assert_eq!(keys, expected_keys);

    let _ = std::fs::remove_dir_all(&workspace);
    let _ = std::fs::remove_dir_all(&output);
}

#[test]
fn a_failed_program_writes_a_failed_artifact_with_the_error() {
    let workspace = temp_dir("panic-workspace");
    let output = temp_dir("panic-output");
    let source = r#"
use harness_strategy_sdk::{export_screening_program, ScreenContext, ScreenRow, ScreeningProgram};
use harness_strategy_sdk::serde_json::Value;

struct Boom;

impl ScreeningProgram for Boom {
    fn new(_parameters: Value) -> Self { Self }
    fn screen(&mut self, _ctx: &ScreenContext) -> Vec<ScreenRow> {
        panic!("authored screening program panicked");
    }
}

export_screening_program!(Boom);
"#;
    let configuration = config(source, json!({}));

    let (outcome, artifact_path) = run_screening_configuration(
        &configuration,
        &FakeSource { fail: false },
        &ScreenRunOptions {
            workspace_root: Some(workspace.clone()),
        },
        Some(&output),
    )
    .expect("run");

    assert_eq!(outcome.status, ScreenStatus::Failed);
    assert!(!outcome.completed());
    assert!(outcome.row_count() == 0);
    let message = outcome.error().expect("error");
    assert!(
        message.contains("authored screening program panicked"),
        "unexpected error: {message}"
    );

    let document: Value =
        serde_json::from_str(&std::fs::read_to_string(&artifact_path).expect("artifact"))
            .expect("artifact json");
    assert_eq!(document["status"], "failed");
    assert_eq!(document["rows"], json!([]));
    assert!(document["error"]
        .as_str()
        .expect("error string")
        .contains("panicked"));
    assert_eq!(document["run_id"], configuration.run_id());
    assert_eq!(
        document["provenance"]["source_digest"].as_str(),
        Some(configuration.bundle_hash.as_str())
    );

    let _ = std::fs::remove_dir_all(&workspace);
    let _ = std::fs::remove_dir_all(&output);
}

#[test]
fn a_data_source_failure_writes_a_failed_artifact() {
    let workspace = temp_dir("data-workspace");
    let output = temp_dir("data-output");
    let configuration = config(SCREENING_SOURCE, json!({}));

    let (outcome, artifact_path) = run_screening_configuration(
        &configuration,
        &FakeSource { fail: true },
        &ScreenRunOptions {
            workspace_root: Some(workspace.clone()),
        },
        Some(&output),
    )
    .expect("run");

    assert_eq!(outcome.status, ScreenStatus::Failed);
    assert!(outcome
        .error()
        .is_some_and(|message| message.contains("no bar coverage")));

    let document: Value =
        serde_json::from_str(&std::fs::read_to_string(&artifact_path).expect("artifact"))
            .expect("artifact json");
    assert_eq!(document["status"], "failed");
    assert_eq!(document["rows"], json!([]));

    let _ = std::fs::remove_dir_all(&workspace);
    let _ = std::fs::remove_dir_all(&output);
}

#[test]
fn identical_inputs_produce_byte_identical_artifacts() {
    let workspace = temp_dir("stable-workspace");
    let first_output = temp_dir("stable-first");
    let second_output = temp_dir("stable-second");
    let configuration = config(SCREENING_SOURCE, json!({"label": "fixture"}));

    let (first_outcome, first_path) = run_screening_configuration(
        &configuration,
        &FakeSource { fail: false },
        &ScreenRunOptions {
            workspace_root: Some(workspace.clone()),
        },
        Some(&first_output),
    )
    .expect("first run");
    let (second_outcome, second_path) = run_screening_configuration(
        &configuration,
        &FakeSource { fail: false },
        &ScreenRunOptions {
            workspace_root: Some(workspace.clone()),
        },
        Some(&second_output),
    )
    .expect("second run");

    assert_eq!(first_outcome.run_id, second_outcome.run_id);
    assert_eq!(
        std::fs::read(&first_path).expect("first artifact"),
        std::fs::read(&second_path).expect("second artifact")
    );

    let _ = std::fs::remove_dir_all(&workspace);
    let _ = std::fs::remove_dir_all(&first_output);
    let _ = std::fs::remove_dir_all(&second_output);
}
