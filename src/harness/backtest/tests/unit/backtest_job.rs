use super::*;

fn temp_dir(name: &str) -> PathBuf {
    std::env::temp_dir().join(format!(
        "harness-backtest-job-{}-{name}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ))
}

#[test]
fn parses_shell_and_scalars() {
    assert_eq!(
        parse_shell("shell: backtest\n").as_deref(),
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
fn rejects_wrong_shell_and_missing_config() {
    let directory = temp_dir("reject");
    std::fs::create_dir_all(&directory).expect("dir");
    let api = directory.join("api.yaml");
    std::fs::write(&api, "shell: api\n").expect("write");
    assert_eq!(
        run_backtest_job(&api, None),
        Err(BacktestJobError::WrongShell("api".to_string()))
    );
    assert!(matches!(
        run_backtest_job(&directory.join("missing.yaml"), None),
        Err(BacktestJobError::Io(_))
    ));
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn runs_end_to_end_and_writes_the_artifact_set() {
    let directory = temp_dir("run");
    std::fs::create_dir_all(&directory).expect("dir");
    let config = directory.join("backtest.yaml");
    std::fs::write(&config, "shell: backtest\nrun_id: bt-e2e\nseed: 7\n").expect("write");
    let output = directory.join("out");
    let summary = run_backtest_job(&config, Some(&output)).expect("job");
    assert_eq!(summary.run_id, "bt-e2e");
    assert_eq!(summary.status, "completed");
    assert_eq!(summary.market_events, 36);
    assert!(summary.artifact_path.exists());
    let document = std::fs::read_to_string(&summary.artifact_path).expect("read");
    assert!(document.contains("\"manifest\""));
    assert!(document.contains("\"counts\""));
    assert_eq!(summary.artifact_digest.len(), 64);
    // The summary renders the CLI lines deterministically.
    assert_eq!(summary.stdout_lines().len(), 9);
    // The complete end-to-end artifact set is present and self-contained.
    assert!(summary.metrics_path.exists());
    assert!(summary.report_path.exists());
    assert!(summary.artifact_dir.join("manifest.parquet").exists());
    let metrics = std::fs::read_to_string(&summary.metrics_path).expect("metrics");
    assert!(metrics.contains("\"equity\""));
    assert!(metrics.contains("\"execution\""));
    let report = std::fs::read_to_string(&summary.report_path).expect("report");
    assert!(report.starts_with("<!doctype html>"));
    assert_eq!(summary.recorder_digest.len(), 64);
    assert_eq!(summary.artifact_paths().len(), 4);
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn runner_seam_runs_a_job_with_a_configured_output_dir() {
    let directory = temp_dir("seam");
    std::fs::create_dir_all(&directory).expect("dir");
    let config = directory.join("backtest.yaml");
    std::fs::write(&config, "shell: backtest\nrun_id: bt-seam\nseed: 3\n").expect("write");
    let output = directory.join("out");
    let runner = BacktestJobRunner::with_output_dir(output.clone());
    assert_eq!(runner.output_dir(), Some(output.as_path()));
    let summary = runner.run(&config).expect("job");
    assert_eq!(summary.run_id, "bt-seam");
    assert!(summary.report_path.starts_with(&output));
    let _ = std::fs::remove_dir_all(&directory);
}
