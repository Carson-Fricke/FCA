use super::*;
use crate::backtest::errors::BacktestError;
use crate::backtest::job::load_screen_config;
use serde_json::json;

const SOURCE: &str = "use harness_strategy_sdk::{export_screening_program, ScreenContext, ScreenRow, ScreeningProgram};\n\nstruct Empty;\n\nimpl ScreeningProgram for Empty {\n    fn new(_parameters: serde_json::Value) -> Self { Self }\n    fn screen(&mut self, _ctx: &ScreenContext) -> Vec<ScreenRow> { Vec::new() }\n}\n\nexport_screening_program!(Empty);\n";

fn document(source: &str) -> serde_json::Value {
    let digest = crate::sha256::digest_hex(source.as_bytes());
    json!({
        "schema_version": 1,
        "shell": "screen",
        "name": "close-screen-example",
        "as_of": "2024-01-19",
        "bar_interval": "1d",
        "lookback_days": 20,
        "rank_metric": "close",
        "universe_revision": "sp500-2024-01",
        "source": source,
        "source_sha256": digest,
        "bundle_hash": digest,
        "definition_id": format!("sha256:{}", "a".repeat(64)),
        "definition_hash": "b".repeat(64),
        "parameters": {"lookback": 20, "threshold": "0.05"},
    })
}

fn config() -> ScreenConfig {
    ScreenConfig::from_json_value(&document(SOURCE)).expect("valid screen config")
}

#[test]
fn parses_the_frozen_json_wire_shape() {
    let configuration = config();
    assert_eq!(configuration.schema_version, 1);
    assert_eq!(configuration.shell, "screen");
    assert_eq!(configuration.name, "close-screen-example");
    assert_eq!(configuration.as_of, "2024-01-19");
    assert_eq!(configuration.bar_interval, "1d");
    assert_eq!(configuration.lookback_days, 20);
    assert_eq!(configuration.rank_metric, "close");
    assert_eq!(
        configuration.universe_revision.as_deref(),
        Some("sp500-2024-01")
    );
    assert_eq!(configuration.source, SOURCE);
    assert_eq!(configuration.source_digest(), configuration.bundle_hash);
    assert_eq!(configuration.source_sha256, configuration.bundle_hash);
    assert_eq!(
        configuration.parameters,
        json!({"lookback": 20, "threshold": "0.05"})
    );
    assert_eq!(configuration.window_start(), "2023-12-30");
    assert_eq!(configuration.window_end(), "2024-01-19");
}

#[test]
fn run_id_is_deterministic_and_content_addressed() {
    let first = config();
    let second = ScreenConfig::from_json_value(&document(SOURCE)).expect("again");
    assert_eq!(first.run_id(), second.run_id());
    assert!(first.run_id().starts_with("screen-"));
    assert_eq!(first.run_id().len(), "screen-".len() + 16);
    assert_eq!(first.configuration_digest().len(), 64);

    let mut other = document(SOURCE);
    other["parameters"] = json!({"lookback": 21});
    let changed = ScreenConfig::from_json_value(&other).expect("changed");
    assert_ne!(first.run_id(), changed.run_id());
}

#[test]
fn normalizes_crlf_before_hashing_the_source() {
    let mut document = document(SOURCE);
    document["source"] = json!(SOURCE.replace('\n', "\r\n"));
    let configuration = ScreenConfig::from_json_value(&document).expect("crlf config");
    assert_eq!(configuration.source, SOURCE);
    assert_eq!(configuration.bundle_hash, configuration.source_digest());
}

#[test]
fn rejects_a_bundle_hash_mismatch() {
    let mut document = document(SOURCE);
    document["bundle_hash"] = json!("c".repeat(64));
    let error = ScreenConfig::from_json_value(&document).expect_err("mismatch");
    assert!(
        matches!(error, BacktestError::Configuration(ref message) if message.contains("bundle_hash")),
        "unexpected error: {error}"
    );
}

#[test]
fn rejects_a_source_sha256_mismatch() {
    let mut document = document(SOURCE);
    document["source_sha256"] = json!("c".repeat(64));
    let error = ScreenConfig::from_json_value(&document).expect_err("mismatch");
    assert!(
        matches!(error, BacktestError::Configuration(ref message) if message.contains("source_sha256")),
        "unexpected error: {error}"
    );
}

#[test]
fn rejects_unknown_keys_and_bad_field_values() {
    let mut unknown = document(SOURCE);
    unknown["run_id"] = json!("not-allowed");
    assert!(ScreenConfig::from_json_value(&unknown).is_err());

    for (field, value) in [
        ("shell", json!("backtest")),
        ("bar_interval", json!("1h")),
        ("lookback_days", json!(0)),
        ("as_of", json!("2024-02-30")),
        ("definition_id", json!("abc")),
        ("definition_hash", json!("A".repeat(64))),
        ("rank_metric", json!("  ")),
        ("source", json!("   ")),
    ] {
        let mut document = document(SOURCE);
        document[field] = value;
        assert!(
            ScreenConfig::from_json_value(&document).is_err(),
            "field {field} should have been rejected"
        );
    }

    let mut parameters = document(SOURCE);
    parameters["parameters"] = json!([1, 2]);
    assert!(ScreenConfig::from_json_value(&parameters).is_err());
}

#[test]
fn parses_a_yaml_document_with_inline_source() {
    let source = "line-1\nline-2";
    let digest = crate::sha256::digest_hex(source.as_bytes());
    let yaml = format!(
        "schema_version: 1\nshell: screen\nname: yaml-screen\nas_of: 2024-01-19\n\
         bar_interval: 1min\nlookback_days: 5\nrank_metric: close\n\
         universe_revision: null\ndefinition_id: sha256:{zeros}\n\
         definition_hash: {hash}\nbundle_hash: {digest}\nsource_sha256: {digest}\n\
         parameters:\n  lookback: 5\nsource: \"line-1\\nline-2\"\n",
        zeros = "0".repeat(64),
        hash = "1".repeat(64),
    );
    let configuration = ScreenConfig::from_yaml(&yaml).expect("yaml config");
    assert_eq!(configuration.source, source);
    assert_eq!(configuration.parameters, json!({"lookback": 5}));
    assert_eq!(configuration.universe_revision, None);
}

#[test]
fn load_screen_config_resolves_source_path_and_rejects_other_shells() {
    let directory = std::env::temp_dir().join(format!(
        "harness-screen-config-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&directory).expect("temp dir");
    let source_path = directory.join("screener.rs");
    std::fs::write(&source_path, SOURCE).expect("write source");
    let digest = crate::sha256::digest_hex(SOURCE.as_bytes());

    let json_path = directory.join("screen.json");
    let mut document = document(SOURCE);
    document.as_object_mut().expect("object").remove("source");
    document["source_path"] = json!("screener.rs");
    std::fs::write(
        &json_path,
        serde_json::to_vec(&document).expect("serialize"),
    )
    .expect("write config");
    let configuration = load_screen_config(&json_path).expect("load json config");
    assert_eq!(configuration.source, SOURCE);
    assert_eq!(configuration.bundle_hash, digest);

    let yaml_path = directory.join("screen.yaml");
    std::fs::write(
        &yaml_path,
        format!(
            "schema_version: 1\nshell: screen\nname: yaml-screen\nas_of: 2024-01-19\n\
             bar_interval: 1min\nlookback_days: 5\nrank_metric: close\n\
             definition_id: sha256:{zeros}\ndefinition_hash: {hash}\n\
             bundle_hash: {digest}\nsource_path: screener.rs\n",
            zeros = "0".repeat(64),
            hash = "1".repeat(64),
        ),
    )
    .expect("write yaml config");
    let configuration = load_screen_config(&yaml_path).expect("load yaml config");
    assert_eq!(configuration.source, SOURCE);

    let backtest_path = directory.join("backtest.json");
    std::fs::write(&backtest_path, "{\"shell\":\"backtest\"}").expect("write wrong shell");
    let error = load_screen_config(&backtest_path).expect_err("wrong shell");
    assert!(
        matches!(error, crate::backtest::job::BacktestJobError::WrongScreenShell(ref shell) if shell == "backtest"),
        "unexpected error: {error}"
    );

    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn a_bare_source_path_rejects_without_a_configuration_directory() {
    let mut document = document(SOURCE);
    document.as_object_mut().expect("object").remove("source");
    document["source_path"] = json!("screener.rs");
    let error = ScreenConfig::from_json_value(&document).expect_err("no base dir");
    assert!(
        matches!(error, BacktestError::Configuration(ref message) if message.contains("source_path")),
        "unexpected error: {error}"
    );
}

#[test]
fn loads_the_committed_template_with_its_embedded_source() {
    let template = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("config")
        .join("harness")
        .join("screener.yaml");
    let configuration = load_screen_config(&template).expect("template loads");
    assert_eq!(configuration.shell, "screen");
    assert_eq!(configuration.source_digest(), configuration.bundle_hash);
    assert!(configuration.source.contains("export_screening_program!"));
    assert_eq!(configuration.window_start(), "2023-12-30");

    let text = std::fs::read_to_string(&template).expect("template text");
    assert!(ScreenConfig::from_yaml(&text).is_ok());
}
