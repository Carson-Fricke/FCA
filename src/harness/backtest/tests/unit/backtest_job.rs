use super::*;

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

const MISSING_INTERVAL_ERROR: &str = "study configuration requires data.bar_interval (1min or 1d)";

fn temp_dir(name: &str) -> PathBuf {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let unique = COUNTER.fetch_add(1, Ordering::Relaxed);
    let path = std::env::temp_dir().join(format!(
        "harness-backtest-job-{}-{name}-{unique}",
        std::process::id()
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

#[test]
fn parses_shell_and_scalars() {
    assert_eq!(
        parse_shell("shell: backtest\n").as_deref(),
        Some("backtest")
    );
    assert_eq!(
        parse_shell(r#"{"schema_version":1,"shell":"backtest","run_id":"r"}"#).as_deref(),
        Some("backtest")
    );
    assert_eq!(parse_shell("market_data:\n  shell: nested\n"), None);
    assert_eq!(
        parse_scalar("run_id: bt-1\n", "run_id").as_deref(),
        Some("bt-1")
    );
    assert_eq!(
        parse_scalar("\"seed\": 42\n", "seed").as_deref(),
        Some("42")
    );
}

#[test]
fn parses_nested_section_scalars() {
    let text = "shell: backtest\ndata:\n  snapshot_id: lake-v1\n  fingerprint: bbbb\n  symbols: [SPY]\nseed: 1\n";
    assert_eq!(
        parse_nested_scalar(text, "data", "snapshot_id").as_deref(),
        Some("lake-v1")
    );
    assert_eq!(
        parse_nested_scalar(text, "data", "fingerprint").as_deref(),
        Some("bbbb")
    );
    assert_eq!(parse_nested_scalar(text, "data", "absent"), None);
    assert_eq!(parse_nested_scalar(text, "other", "snapshot_id"), None);
}

#[test]
fn parses_iso_timestamps_to_epoch_nanoseconds() {
    assert_eq!(
        parse_timestamp_ns("2024-01-02T14:30:00Z"),
        Some(
            crate::backtest::fixture::days_from_civil(2024, 1, 2) * ONE_DAY_NS
                + 14 * 3_600_000_000_000
                + 30 * 60_000_000_000
        )
    );
    assert_eq!(
        parse_timestamp_ns("2024-01-02T14:30:00+00:00"),
        parse_timestamp_ns("2024-01-02T14:30:00Z")
    );
    assert_eq!(parse_timestamp_ns("not-a-date"), None);
}

#[test]
fn study_rejects_a_missing_bar_interval_in_json_and_yaml() {
    let directory = temp_dir("study-missing-interval");
    let json_path = directory.join("study.json");
    std::fs::write(
        &json_path,
        r#"{"schema_version":1,"shell":"study","run_id":"study-missing","data":{"symbols":["SPY"]}}"#,
    )
    .expect("write json");
    let error =
        load_study_config(&json_path).expect_err("a study without data.bar_interval must fail");
    assert_eq!(error.to_string(), MISSING_INTERVAL_ERROR);

    let yaml_path = directory.join("study.yaml");
    std::fs::write(
        &yaml_path,
        "schema_version: 1\nshell: study\nrun_id: study-missing\ndata:\n  symbols: [SPY]\n",
    )
    .expect("write yaml");
    let error =
        load_study_config(&yaml_path).expect_err("a study without data.bar_interval must fail");
    assert_eq!(error.to_string(), MISSING_INTERVAL_ERROR);
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn study_rejects_a_non_string_bar_interval() {
    let directory = temp_dir("study-scalar-interval");
    let path = directory.join("study.json");
    std::fs::write(
        &path,
        r#"{"schema_version":1,"shell":"study","run_id":"study-scalar","data":{"symbols":["SPY"],"bar_interval":60}}"#,
    )
    .expect("write json");
    let error = load_study_config(&path).expect_err("a non-string interval must fail");
    assert_eq!(error.to_string(), MISSING_INTERVAL_ERROR);
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn study_accepts_canonical_bar_interval_tokens() {
    let directory = temp_dir("study-valid-interval");
    for token in ["1min", "1d"] {
        let json_path = directory.join(format!("study-{token}.json"));
        std::fs::write(
            &json_path,
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 1,
                "shell": "study",
                "run_id": format!("study-{token}"),
                "data": {"symbols": ["SPY"], "bar_interval": token}
            }))
            .expect("config json"),
        )
        .expect("write json");
        let configuration = load_study_config(&json_path).expect("canonical token");
        assert_eq!(configuration.data_bar_interval, token);

        let yaml_path = directory.join(format!("study-{token}.yaml"));
        std::fs::write(
            &yaml_path,
            format!(
                "schema_version: 1\nshell: study\nrun_id: study-{token}\ndata:\n  \
                 symbols: [SPY]\n  bar_interval: {token}\n"
            ),
        )
        .expect("write yaml");
        let configuration = load_study_config(&yaml_path).expect("canonical token");
        assert_eq!(configuration.data_bar_interval, token);
    }
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn study_rejects_an_interval_outside_the_canonical_vocabulary() {
    let directory = temp_dir("study-invalid-interval");
    let path = directory.join("study.json");
    std::fs::write(
        &path,
        r#"{"schema_version":1,"shell":"study","run_id":"study-invalid","data":{"symbols":["SPY"],"bar_interval":"1h"}}"#,
    )
    .expect("write json");
    let error = load_study_config(&path).expect_err("1h must fail");
    let message = error.to_string();
    assert!(
        message.contains("study data.bar_interval is invalid"),
        "{message}"
    );
    assert!(message.contains("1min, 1d"), "{message}");
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn backtest_keeps_the_default_bar_interval_when_absent() {
    let directory = temp_dir("backtest-default-interval");
    let json_path = directory.join("backtest.json");
    std::fs::write(
        &json_path,
        r#"{"schema_version":1,"shell":"backtest","run_id":"backtest-default","data":{"symbols":["SPY"]}}"#,
    )
    .expect("write json");
    let configuration = load_backtest_config(&json_path).expect("backtests keep the default");
    assert_eq!(configuration.data_bar_interval, "1min");

    let yaml_path = directory.join("backtest.yaml");
    std::fs::write(
        &yaml_path,
        "schema_version: 1\nshell: backtest\nrun_id: backtest-default\ndata:\n  symbols: [SPY]\n",
    )
    .expect("write yaml");
    let configuration = load_backtest_config(&yaml_path).expect("backtests keep the default");
    assert_eq!(configuration.data_bar_interval, "1min");
    let _ = std::fs::remove_dir_all(&directory);
}
