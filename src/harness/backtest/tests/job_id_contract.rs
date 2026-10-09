use std::path::PathBuf;

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::job::{
    load_backtest_config_with_job_id, load_screen_config_with_job_id,
    load_study_config_with_job_id, overlay_job_id_json,
};
use backtest::backtest::screen::ScreenConfig;
use serde_json::{json, Value};

fn replay_document(shell: &str, job_id: Option<&str>) -> Value {
    let mut document = json!({
        "schema_version": 1,
        "shell": shell,
        "start": "2024-01-02T14:30:00Z",
        "end": "2024-01-04T21:00:00Z",
        "data": {
            "symbols": ["SPY"],
            "subscriptions": [{"data_type": "quote", "symbols": ["SPY"]}]
        }
    });
    if let Some(job_id) = job_id {
        document["job_id"] = json!(job_id);
    }
    document
}

fn screen_document(job_id: Option<&str>) -> Value {
    let source = "// screening source\n";
    let digest = backtest::sha256::digest_hex(source.as_bytes());
    let mut document = json!({
        "schema_version": 1,
        "shell": "screen",
        "name": "job-id-contract",
        "as_of": "2024-01-19",
        "bar_intervals": ["1d"],
        "lookback_days": 20,
        "rank_metric": "close",
        "source": source,
        "bundle_hash": digest,
        "definition_id": format!("sha256:{}", "0".repeat(64)),
        "definition_hash": "0".repeat(64)
    });
    if let Some(job_id) = job_id {
        document["job_id"] = json!(job_id);
    }
    document
}

fn write_temp(name: &str, document: &Value) -> PathBuf {
    let path =
        std::env::temp_dir().join(format!("harness-job-id-{}-{name}.json", std::process::id()));
    std::fs::write(&path, document.to_string()).unwrap();
    path
}

#[test]
fn replay_configuration_requires_job_id() {
    for shell in ["backtest", "study"] {
        let document = replay_document(shell, None);
        let text = document.to_string();
        let error = BacktestConfig::from_yaml(&text).unwrap_err();
        assert!(error.to_string().contains("job_id"), "{shell}: {error}");
        assert!(
            config::replay::validate_document(&document)
                .unwrap_err()
                .to_string()
                .contains("job_id"),
            "{shell}"
        );
        assert!(BacktestConfig::from_yaml(
            &replay_document(shell, Some("job-present")).to_string()
        )
        .is_ok());
    }
}

#[test]
fn screen_configuration_requires_job_id() {
    let error = ScreenConfig::from_json_value(&screen_document(None)).unwrap_err();
    assert!(error.to_string().contains("job_id"), "{error}");
    assert!(ScreenConfig::from_json_value(&screen_document(Some("job-screen"))).is_ok());
}

#[test]
fn loaders_overlay_an_explicit_job_id_on_a_document_that_lacks_one() {
    let path = write_temp("backtest-overlay", &replay_document("backtest", None));
    let configuration = load_backtest_config_with_job_id(&path, Some("job-overlay")).unwrap();
    assert_eq!(configuration.job_id, "job-overlay");
    std::fs::remove_file(path).unwrap();

    let path = write_temp("study-overlay", &replay_document("study", None));
    let configuration = load_study_config_with_job_id(&path, Some("job-study-overlay")).unwrap();
    assert_eq!(configuration.job_id, "job-study-overlay");
    std::fs::remove_file(path).unwrap();

    let path = write_temp("screen-overlay", &screen_document(None));
    let configuration = load_screen_config_with_job_id(&path, Some("job-screen-overlay")).unwrap();
    assert_eq!(configuration.job_id, "job-screen-overlay");
    std::fs::remove_file(path).unwrap();
}

#[test]
fn loaders_apply_the_submitted_job_id_over_the_document() {
    let path = write_temp(
        "backtest-overlay-doc",
        &replay_document("backtest", Some("job-doc")),
    );
    assert_eq!(
        load_backtest_config_with_job_id(&path, Some("job-submitted"))
            .unwrap()
            .job_id,
        "job-submitted"
    );
    assert_eq!(
        load_backtest_config_with_job_id(&path, Some("job-doc"))
            .unwrap()
            .job_id,
        "job-doc"
    );
    std::fs::remove_file(path).unwrap();

    let path = write_temp(
        "screen-overlay-doc",
        &screen_document(Some("job-screen-doc")),
    );
    assert_eq!(
        load_screen_config_with_job_id(&path, Some("job-screen-submitted"))
            .unwrap()
            .job_id,
        "job-screen-submitted"
    );
    std::fs::remove_file(path).unwrap();
}

#[test]
fn overlay_helper_rejects_unsafe_submitted_ids() {
    for job_id in ["", " ", "a/b", "a\\b", "..", &"x".repeat(129)] {
        let mut document = replay_document("backtest", None);
        assert!(
            overlay_job_id_json(&mut document, Some(job_id)).is_err(),
            "{job_id:?}"
        );
    }
}

#[test]
fn shipped_screener_configuration_carries_an_explicit_job_id() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("config")
        .join("harness")
        .join("screener.yaml");
    let configuration =
        backtest::backtest::job::load_screen_config(&path).expect("shipped screener config");
    assert_eq!(configuration.job_id, "job-screen-example");
}

#[test]
fn screen_configuration_freezes_multiple_canonical_intervals() {
    let mut document = screen_document(Some("job-multi-screen"));
    document["bar_intervals"] = json!(["15min", "1d", "1min", "15min"]);
    let config = ScreenConfig::from_json_value(&document).unwrap();
    assert_eq!(config.bar_intervals, ["15min", "1d", "1min"]);
    let before = config.configuration_digest();
    document["bar_intervals"] = json!(["1min", "1d"]);
    assert_ne!(
        before,
        ScreenConfig::from_json_value(&document)
            .unwrap()
            .configuration_digest()
    );
    document["bar_intervals"] = json!(["1s"]);
    assert!(ScreenConfig::from_json_value(&document).is_err());
}
