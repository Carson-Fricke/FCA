use super::*;

/// Serializes the tests that mutate the process-wide lake environment.
pub(crate) static LAKE_ENV_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[derive(Default)]
struct RecordingRunner {
    seen: std::cell::RefCell<Vec<BacktestJobArgs>>,
    summary: Option<BacktestRunSummary>,
}

impl BacktestJobRunner for RecordingRunner {
    fn run(&self, args: &BacktestJobArgs) -> Result<BacktestRunSummary, BacktestJobError> {
        self.seen.borrow_mut().push(args.clone());
        match &self.summary {
            Some(summary) => Ok(summary.clone()),
            None => Err(BacktestJobError::EngineNotPorted),
        }
    }
}

fn completed_summary() -> BacktestRunSummary {
    BacktestRunSummary {
        run_id: "bt-test".to_string(),
        status: "completed".to_string(),
        manifest_hash: "m".to_string(),
        artifact_digest: "a".to_string(),
        outcome_digest: "o".to_string(),
        market_events: 12,
        orders: 3,
        fills: 2,
        artifact_path: PathBuf::from(".local/backtests/bt-test/backtest.json"),
        reason: None,
    }
}

fn temp_config(name: &str, body: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-cli-test-{}-{name}.yaml",
        std::process::id()
    ));
    fs::write(&path, body).expect("write temp config");
    path
}

#[test]
fn root_help_lists_process_groups() {
    let help = root_help();
    assert!(help.contains("Run a quant-harness process"));
    assert!(help.contains("api"));
    assert!(help.contains("backtest-job"));
    assert!(help.contains("screen-job"));
    assert!(help.contains("trader"));
}

#[test]
fn bare_harness_shows_root_help() {
    let cli = HarnessCli::try_parse_from(["harness"]).expect("parse");
    let result = run(
        cli,
        &UnportedBacktestJobRunner,
        &UnportedStudyJobRunner,
        &UnavailableScreenJobRunner,
    );
    match result {
        CommandResult::Help(help) => assert!(help.contains("backtest-job")),
        other => panic!("expected help, got {other:?}"),
    }
}

#[test]
fn backtest_job_parses_its_flags() {
    let cli = HarnessCli::try_parse_from([
        "harness",
        "backtest-job",
        "--config",
        "backtest.yaml",
        "--output-dir",
        "out",
    ])
    .expect("parse");
    let Some(HarnessCommand::BacktestJob(args)) = cli.command else {
        panic!("expected backtest-job");
    };
    assert_eq!(args.config, Some(PathBuf::from("backtest.yaml")));
    assert_eq!(args.output_dir, Some(PathBuf::from("out")));
}

#[test]
fn backtest_and_study_jobs_reject_the_removed_data_dir_flag() {
    for flag in ["--data-dir", "--data_dir"] {
        let backtest = HarnessCli::try_parse_from([
            "harness",
            "backtest-job",
            "--config",
            "backtest.yaml",
            flag,
            "lake",
        ]);
        assert!(backtest.is_err(), "backtest-job accepted {flag}");

        let study = HarnessCli::try_parse_from([
            "harness",
            "study-job",
            "--config",
            "study.yaml",
            flag,
            "lake",
        ]);
        assert!(study.is_err(), "study-job accepted {flag}");
    }
}

#[test]
fn backtest_job_requires_a_configuration() {
    let cli = HarnessCli::try_parse_from(["harness", "backtest-job"]).expect("parse");
    let result = run(
        cli,
        &UnportedBacktestJobRunner,
        &UnportedStudyJobRunner,
        &UnavailableScreenJobRunner,
    );
    match result {
        CommandResult::UsageError(ref message) => {
            assert_eq!(result.exit_code(), 2);
            assert!(message.contains("--config"));
        }
        other => panic!("expected usage error, got {other:?}"),
    }
}

#[test]
fn backtest_job_rejects_a_non_backtest_shell() {
    let path = temp_config("api", "shell: api\n");
    let cli = HarnessCli::try_parse_from([
        "harness",
        "backtest-job",
        "--config",
        path.to_str().expect("utf8 path"),
    ])
    .expect("parse");
    let result = run(
        cli,
        &UnportedBacktestJobRunner,
        &UnportedStudyJobRunner,
        &UnavailableScreenJobRunner,
    );
    match result {
        CommandResult::UsageError(ref message) => {
            assert_eq!(result.exit_code(), 2);
            assert!(message.contains("shell must be 'backtest'"));
            assert!(message.contains("'api'"));
        }
        other => panic!("expected usage error, got {other:?}"),
    }
    let _ = fs::remove_file(path);
}

#[test]
fn backtest_job_drives_the_injected_runner() {
    let path = temp_config("backtest", "shell: backtest\n");
    let runner = RecordingRunner {
        seen: std::cell::RefCell::new(Vec::new()),
        summary: Some(completed_summary()),
    };
    let cli = HarnessCli::try_parse_from([
        "harness",
        "backtest-job",
        "--config",
        path.to_str().expect("utf8 path"),
        "--output-dir",
        "out",
    ])
    .expect("parse");
    let result = run(
        cli,
        &runner,
        &UnportedStudyJobRunner,
        &UnavailableScreenJobRunner,
    );
    match result {
        CommandResult::Completed { ref stdout } => {
            assert_eq!(result.exit_code(), 0);
            assert!(stdout.contains("status: completed"));
            assert!(stdout.contains("run_id: bt-test"));
            assert!(stdout.contains("artifacts: .local/backtests/bt-test/backtest.json"));
        }
        other => panic!("expected completion, got {other:?}"),
    }
    let seen = runner.seen.borrow();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].output_dir, Some(PathBuf::from("out")));
    let _ = fs::remove_file(path);
}

#[test]
fn backtest_job_reports_engine_failure_with_exit_one() {
    let path = temp_config("unported", "shell: backtest\n");
    let cli = HarnessCli::try_parse_from([
        "harness",
        "backtest-job",
        "--config",
        path.to_str().expect("utf8 path"),
    ])
    .expect("parse");
    let result = run(
        cli,
        &UnportedBacktestJobRunner,
        &UnportedStudyJobRunner,
        &UnavailableScreenJobRunner,
    );
    match result {
        CommandResult::Failed { ref stderr, .. } => {
            assert_eq!(result.exit_code(), 1);
            assert!(stderr.contains("Backtest failed"));
        }
        other => panic!("expected failure, got {other:?}"),
    }
    let _ = fs::remove_file(path);
}

#[test]
fn standalone_backtest_job_help_matches() {
    let help = BacktestJobCli::command().render_help().to_string();
    assert!(help.contains("Run one deterministic backtest job"));
    assert!(help.contains("--config"));
    assert!(help.contains("--output-dir"));
    assert!(
        !help.contains("--data-dir"),
        "the removed data-dir surface must not appear: {help}"
    );
    assert!(
        !help.contains("market-lake") && !help.contains("HARNESS_DATA_DIR"),
        "the help must not advertise local lake directories: {help}"
    );
}

#[test]
fn standalone_study_job_help_matches() {
    let help = StudyJobCli::command().render_help().to_string();
    assert!(help.contains("--config"));
    assert!(help.contains("--output-dir"));
    assert!(!help.contains("--data-dir"), "{help}");
}

#[test]
fn standalone_backtest_job_dispatches() {
    let path = temp_config("standalone", "shell: backtest\n");
    let runner = RecordingRunner {
        seen: std::cell::RefCell::new(Vec::new()),
        summary: Some(completed_summary()),
    };
    let cli = BacktestJobCli::try_parse_from([
        "backtest-job",
        "--config",
        path.to_str().expect("utf8 path"),
    ])
    .expect("parse");
    let result = run_backtest_job(cli, &runner);
    assert_eq!(result.exit_code(), 0);
    assert_eq!(runner.seen.borrow().len(), 1);
    let _ = fs::remove_file(path);
}

#[test]
fn trader_dispatches_to_the_supervision_runner() {
    let cli = HarnessCli::try_parse_from(["harness", "trader"]).expect("parse");
    match run(
        cli,
        &UnportedBacktestJobRunner,
        &UnportedStudyJobRunner,
        &UnavailableScreenJobRunner,
    ) {
        CommandResult::RunTrader(args) => {
            assert_eq!(args.config, PathBuf::from("config/harness/trader.yaml"));
            assert!(args.command.is_none());
        }
        other => panic!("expected trader run, got {other:?}"),
    }
}

#[test]
fn api_and_mcp_dispatch_to_their_servers() {
    let api = HarnessCli::try_parse_from(["harness", "api"]).expect("parse");
    match run(
        api,
        &UnportedBacktestJobRunner,
        &UnportedStudyJobRunner,
        &UnavailableScreenJobRunner,
    ) {
        CommandResult::ServeApi(args) => assert!(args.config.is_none()),
        other => panic!("expected api serve, got {other:?}"),
    }

    let mcp = HarnessCli::try_parse_from([
        "harness",
        "mcp",
        "--mode",
        "remote",
        "--api-base-url",
        "https://api.example.test",
        "--enable-trader",
    ])
    .expect("parse");
    match run(
        mcp,
        &UnportedBacktestJobRunner,
        &UnportedStudyJobRunner,
        &UnavailableScreenJobRunner,
    ) {
        CommandResult::ServeMcp(args) => {
            assert_eq!(args.mode, "remote");
            assert!(args.enable_trader);
            assert_eq!(
                args.api_base_url.as_deref(),
                Some("https://api.example.test")
            );
        }
        other => panic!("expected mcp serve, got {other:?}"),
    }
}

#[test]
fn parse_configuration_shell_reads_yaml_and_json() {
    assert_eq!(
        parse_configuration_shell("shell: backtest\n").as_deref(),
        Some("backtest")
    );
    assert_eq!(
        parse_configuration_shell("mode: x\n\"shell\": \"api\"\n").as_deref(),
        Some("api")
    );
    assert_eq!(
        parse_configuration_shell("market_data:\n  shell: nested\n"),
        None
    );
    assert_eq!(parse_configuration_shell("no shell here\n"), None);
}

#[derive(Default)]
struct RecordingScreenRunner {
    seen: std::cell::RefCell<Vec<ScreenJobArgs>>,
    summary: Option<ScreenRunSummary>,
}

impl ScreenJobRunner for RecordingScreenRunner {
    fn run(&self, args: &ScreenJobArgs) -> Result<ScreenRunSummary, ScreenJobError> {
        self.seen.borrow_mut().push(args.clone());
        match &self.summary {
            Some(summary) => Ok(summary.clone()),
            None => Err(ScreenJobError::Engine("not composed".to_string())),
        }
    }
}

fn completed_screen_summary() -> ScreenRunSummary {
    ScreenRunSummary {
        run_id: "screen-0123456789abcdef".to_string(),
        status: "complete".to_string(),
        rows: 4,
        artifact_path: PathBuf::from(".local/screens/screen-0123456789abcdef/screen.json"),
        reason: None,
    }
}

#[test]
fn screen_job_requires_a_configuration() {
    let cli = HarnessCli::try_parse_from(["harness", "screen-job"]).expect("parse");
    let result = run(
        cli,
        &UnportedBacktestJobRunner,
        &UnportedStudyJobRunner,
        &UnavailableScreenJobRunner,
    );
    match result {
        CommandResult::UsageError(ref message) => {
            assert_eq!(result.exit_code(), 2);
            assert!(message.contains("--config"));
        }
        other => panic!("expected usage error, got {other:?}"),
    }
}

#[test]
fn screen_job_rejects_a_non_screen_shell() {
    let path = temp_config("screen-wrong", "shell: backtest\n");
    let cli = HarnessCli::try_parse_from([
        "harness",
        "screen-job",
        "--config",
        path.to_str().expect("utf8 path"),
    ])
    .expect("parse");
    let result = run(
        cli,
        &UnportedBacktestJobRunner,
        &UnportedStudyJobRunner,
        &UnavailableScreenJobRunner,
    );
    match result {
        CommandResult::UsageError(ref message) => {
            assert_eq!(result.exit_code(), 2);
            assert!(message.contains("shell must be 'screen'"));
            assert!(message.contains("'backtest'"));
        }
        other => panic!("expected usage error, got {other:?}"),
    }
    let _ = fs::remove_file(path);
}

#[test]
fn backtest_job_rejects_a_screen_shell() {
    let path = temp_config("screen-as-backtest", "shell: screen\n");
    let cli = HarnessCli::try_parse_from([
        "harness",
        "backtest-job",
        "--config",
        path.to_str().expect("utf8 path"),
    ])
    .expect("parse");
    let result = run(
        cli,
        &UnportedBacktestJobRunner,
        &UnportedStudyJobRunner,
        &UnavailableScreenJobRunner,
    );
    match result {
        CommandResult::UsageError(ref message) => {
            assert_eq!(result.exit_code(), 2);
            assert!(message.contains("shell must be 'backtest'"));
            assert!(message.contains("'screen'"));
        }
        other => panic!("expected usage error, got {other:?}"),
    }
    let _ = fs::remove_file(path);
}

#[test]
fn screen_job_drives_the_injected_runner() {
    let path = temp_config("screen-ok", "shell: screen\n");
    let runner = RecordingScreenRunner {
        seen: std::cell::RefCell::new(Vec::new()),
        summary: Some(completed_screen_summary()),
    };
    let cli = HarnessCli::try_parse_from([
        "harness",
        "screen-job",
        "--config",
        path.to_str().expect("utf8 path"),
        "--output-dir",
        "out",
    ])
    .expect("parse");
    let result = run(
        cli,
        &UnportedBacktestJobRunner,
        &UnportedStudyJobRunner,
        &runner,
    );
    match result {
        CommandResult::Completed { ref stdout } => {
            assert_eq!(result.exit_code(), 0);
            assert!(stdout.contains("status: complete"));
            assert!(stdout.contains("rows: 4"));
            assert!(stdout.contains("run_id: screen-0123456789abcdef"));
        }
        other => panic!("expected completion, got {other:?}"),
    }
    let seen = runner.seen.borrow();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].output_dir, Some(PathBuf::from("out")));
    let _ = fs::remove_file(path);
}

#[test]
fn screen_job_reports_a_failed_run_with_exit_one() {
    let path = temp_config("screen-failed", "shell: screen\n");
    let runner = RecordingScreenRunner {
        seen: std::cell::RefCell::new(Vec::new()),
        summary: Some(ScreenRunSummary {
            status: "failed".to_string(),
            reason: Some("screening data source failed: no bar coverage".to_string()),
            ..completed_screen_summary()
        }),
    };
    let cli = HarnessCli::try_parse_from([
        "harness",
        "screen-job",
        "--config",
        path.to_str().expect("utf8 path"),
    ])
    .expect("parse");
    let result = run(
        cli,
        &UnportedBacktestJobRunner,
        &UnportedStudyJobRunner,
        &runner,
    );
    match result {
        CommandResult::Failed {
            ref stdout,
            ref stderr,
        } => {
            assert_eq!(result.exit_code(), 1);
            assert!(stdout.contains("status: failed"));
            assert!(stderr.contains("no bar coverage"));
        }
        other => panic!("expected failure, got {other:?}"),
    }
    let _ = fs::remove_file(path);
}

#[test]
fn standalone_screen_job_dispatches() {
    let path = temp_config("screen-standalone", "shell: screen\n");
    let runner = RecordingScreenRunner {
        seen: std::cell::RefCell::new(Vec::new()),
        summary: Some(completed_screen_summary()),
    };
    let cli =
        ScreenJobCli::try_parse_from(["screen-job", "--config", path.to_str().expect("utf8 path")])
            .expect("parse");
    let result = run_screen_job(cli, &runner);
    assert_eq!(result.exit_code(), 0);
    assert_eq!(runner.seen.borrow().len(), 1);
    let _ = fs::remove_file(path);
}
