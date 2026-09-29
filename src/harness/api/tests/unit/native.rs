use super::*;
use std::fs;

/// Hold the shared lake-environment lock and restore the token on drop.
struct LakeTokenGuard {
    _guard: std::sync::MutexGuard<'static, ()>,
    saved: Option<String>,
}

impl LakeTokenGuard {
    fn without_token() -> Self {
        let guard = crate::tests::LAKE_ENV_TEST_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let saved = std::env::var("LAKE_API__BEARER_TOKEN").ok();
        std::env::remove_var("LAKE_API__BEARER_TOKEN");
        Self {
            _guard: guard,
            saved,
        }
    }
}

impl Drop for LakeTokenGuard {
    fn drop(&mut self) {
        match &self.saved {
            Some(token) => std::env::set_var("LAKE_API__BEARER_TOKEN", token),
            None => std::env::remove_var("LAKE_API__BEARER_TOKEN"),
        }
    }
}

fn temp_dir(name: &str) -> PathBuf {
    let directory = std::env::temp_dir().join(format!(
        "harness-native-{name}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    fs::create_dir_all(&directory).expect("temp dir");
    directory
}

fn write_config(directory: &std::path::Path, name: &str, document: serde_json::Value) -> PathBuf {
    let path = directory.join(name);
    fs::write(
        &path,
        serde_json::to_vec(&document).expect("serialize config"),
    )
    .expect("write config");
    path
}

#[test]
fn missing_configuration_is_reported() {
    let runner = NativeBacktestJobRunner;
    match runner.run(&BacktestJobArgs::default()) {
        Err(BacktestJobError::Engine(reason)) => {
            assert!(reason.contains("--config"), "{reason}");
        }
        other => panic!("expected an engine error, got {other:?}"),
    }
}

#[test]
fn a_missing_backtest_lake_configuration_fails_loudly() {
    let _guard = LakeTokenGuard::without_token();
    let directory = temp_dir("backtest-lake");
    let config = write_config(
        &directory,
        "backtest.json",
        serde_json::json!({"schema_version": 1, "shell": "backtest", "run_id": "missing-lake"}),
    );
    let runner = NativeBacktestJobRunner;
    match runner.run(&BacktestJobArgs {
        config: Some(config),
        output_dir: Some(directory.join("out")),
    }) {
        Err(BacktestJobError::Engine(reason)) => {
            assert!(
                reason.contains("LAKE_API__BEARER_TOKEN"),
                "the missing lake configuration must fail loudly: {reason}"
            );
        }
        other => panic!("expected a loud lake configuration error, got {other:?}"),
    }
    let _ = fs::remove_dir_all(&directory);
}

#[test]
fn a_missing_study_lake_configuration_fails_loudly() {
    let _guard = LakeTokenGuard::without_token();
    let directory = temp_dir("study-lake");
    let config = write_config(
        &directory,
        "study.json",
        serde_json::json!({
            "schema_version": 1,
            "shell": "study",
            "run_id": "missing-lake",
            "data": {"symbols": ["SPY"], "bar_interval": "1min"}
        }),
    );
    let runner = NativeStudyJobRunner;
    match runner.run(&StudyJobArgs {
        config: Some(config),
        output_dir: Some(directory.join("out")),
    }) {
        Err(BacktestJobError::Engine(reason)) => {
            assert!(
                reason.contains("LAKE_API__BEARER_TOKEN"),
                "the missing lake configuration must fail loudly: {reason}"
            );
        }
        other => panic!("expected a loud lake configuration error, got {other:?}"),
    }
    let _ = fs::remove_dir_all(&directory);
}

#[test]
fn native_screen_runner_fails_loud_without_a_lake_configuration() {
    let _guard = LakeTokenGuard::without_token();
    let directory = temp_dir("screen-lake");
    let source = "fn main() {}\n";
    let digest = backtest::sha256::digest_hex(source.as_bytes());
    let config_path = write_config(
        &directory,
        "screen.json",
        serde_json::json!({
            "schema_version": 1,
            "shell": "screen",
            "name": "native-screen",
            "as_of": "2024-01-19",
            "bar_interval": "1d",
            "lookback_days": 20,
            "rank_metric": "close",
            "source": source,
            "source_sha256": digest,
            "bundle_hash": digest,
            "definition_id": format!("sha256:{}", "a".repeat(64)),
            "definition_hash": "b".repeat(64),
            "parameters": {},
        }),
    );

    let runner = NativeScreenJobRunner;
    match runner.run(&ScreenJobArgs {
        config: Some(config_path),
        output_dir: Some(directory.join("out")),
    }) {
        Err(ScreenJobError::Engine(reason)) => {
            assert!(
                reason.contains("LAKE_API__BEARER_TOKEN"),
                "the missing lake configuration must fail loudly: {reason}"
            );
        }
        other => panic!("expected a loud lake configuration error, got {other:?}"),
    }

    let _ = fs::remove_dir_all(&directory);
}

#[test]
fn native_screen_runner_requires_a_configuration() {
    let runner = NativeScreenJobRunner;
    match runner.run(&ScreenJobArgs::default()) {
        Err(ScreenJobError::Engine(reason)) => {
            assert!(reason.contains("--config"), "{reason}");
        }
        other => panic!("expected an engine error, got {other:?}"),
    }
}
