//! End-to-end backtest job on the Rust stack.
//!
//! Mirrors the `backtest-job` console script: read a configuration document,
//! require the `backtest` shell, run one deterministic backtest through the
//! Rust kernels, and write the canonical `backtest.json` artifact next to the
//! run directory. The `harness-api` binary plugs its [`BacktestJobRunner`]
//! seam into this function; the seam file is integration-owned, so the runner
//! lives here and is exercised directly.
//!
//! [`BacktestJobRunner`]: https://docs.rs/harness-api

use std::path::{Path, PathBuf};

use crate::backtest::config::BacktestConfig;
use crate::backtest::engine::{
    run_backtest, ApproveAll, BacktestOptions, ExecutionModel, ImmediateFill, RiskModel,
    StrategyRuntime, TradeBuyStrategy,
};
use crate::backtest::errors::{BacktestError, ONE_DAY_NS};
use crate::backtest::manifest::BacktestResult;

/// A backtest job failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BacktestJobError {
    /// The configuration document could not be read.
    Io(String),
    /// The document does not carry a `shell` key.
    MissingShell,
    /// The shell is not `backtest`.
    WrongShell(String),
    /// The engine rejected the run.
    Engine(String),
}

impl std::fmt::Display for BacktestJobError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(message) => write!(formatter, "{message}"),
            Self::MissingShell => formatter.write_str("configuration has no 'shell' key"),
            Self::WrongShell(shell) => {
                write!(formatter, "configuration shell must be 'backtest', not '{shell}'")
            }
            Self::Engine(message) => write!(formatter, "{message}"),
        }
    }
}

impl std::error::Error for BacktestJobError {}

impl From<BacktestError> for BacktestJobError {
    fn from(error: BacktestError) -> Self {
        Self::Engine(error.to_string())
    }
}

/// The canonical summary the job prints after a run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BacktestJobSummary {
    /// Run identifier.
    pub run_id: String,
    /// Terminal status (`completed` or `aborted`).
    pub status: String,
    /// Canonical manifest hash.
    pub manifest_hash: String,
    /// Artifact digest.
    pub artifact_digest: String,
    /// Outcome digest.
    pub outcome_digest: String,
    /// Number of market events consumed.
    pub market_events: u64,
    /// Number of orders submitted.
    pub orders: u64,
    /// Number of fills produced.
    pub fills: u64,
    /// Path of the written `backtest.json` artifact.
    pub artifact_path: PathBuf,
    /// Failure reason, present when the run did not complete.
    pub reason: Option<String>,
}

impl BacktestJobSummary {
    /// Render the summary exactly as the CLI writes it to stdout.
    #[must_use]
    pub fn stdout_lines(&self) -> Vec<String> {
        vec![
            format!("run_id: {}", self.run_id),
            format!("status: {}", self.status),
            format!("manifest_hash: {}", self.manifest_hash),
            format!("artifact_digest: {}", self.artifact_digest),
            format!("outcome_digest: {}", self.outcome_digest),
            format!("market_events: {}", self.market_events),
            format!("orders: {}", self.orders),
            format!("fills: {}", self.fills),
            format!("artifacts: {}", self.artifact_path.display()),
        ]
    }
}

/// Read the top-level `shell` scalar from a configuration document.
#[must_use]
pub fn parse_shell(text: &str) -> Option<String> {
    for line in text.lines() {
        let trimmed = line.trim_end();
        if trimmed.trim_start().len() != trimmed.len() {
            continue;
        }
        let rest = trimmed
            .strip_prefix("shell:")
            .or_else(|| trimmed.strip_prefix("\"shell\":"));
        if let Some(rest) = rest {
            let value = rest
                .trim()
                .trim_matches(|character| character == '"' || character == '\'');
            if !value.is_empty() {
                return Some(value.to_string());
            }
        }
    }
    None
}

/// Read a top-level scalar (string or integer) from a configuration document.
#[must_use]
pub fn parse_scalar(text: &str, key: &str) -> Option<String> {
    for line in text.lines() {
        let trimmed = line.trim_end();
        if trimmed.trim_start().len() != trimmed.len() {
            continue;
        }
        let rest = trimmed
            .strip_prefix(&format!("{key}:"))
            .or_else(|| trimmed.strip_prefix(&format!("\"{key}\":")));
        if let Some(rest) = rest {
            let value = rest
                .trim()
                .trim_matches(|character| character == '"' || character == '\'');
            if !value.is_empty() {
                return Some(value.to_string());
            }
        }
    }
    None
}

/// The default synthetic run window: 2024-01-01 to 2024-01-03 UTC.
const DEFAULT_START_NS: i64 = 19_723 * ONE_DAY_NS;
const DEFAULT_END_NS: i64 = 19_725 * ONE_DAY_NS;

/// Run one backtest job using the built-in deterministic strategy seam.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the configuration is missing or invalid,
/// the engine rejects the run, or the artifact cannot be written.
pub fn run_backtest_job(
    config_path: &Path,
    output_dir: Option<&Path>,
) -> Result<BacktestJobSummary, BacktestJobError> {
    let text = std::fs::read_to_string(config_path)
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let shell = parse_shell(&text).ok_or(BacktestJobError::MissingShell)?;
    if shell != "backtest" {
        return Err(BacktestJobError::WrongShell(shell));
    }
    let run_id = parse_scalar(&text, "run_id").unwrap_or_else(|| "backtest-job".to_string());
    let seed = parse_scalar(&text, "seed")
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(7);
    let mut configuration =
        BacktestConfig::new(run_id, DEFAULT_START_NS, DEFAULT_END_NS, seed)?;
    configuration.shell = "backtest".to_string();
    let options = BacktestOptions::default();
    run_job_with(
        &configuration,
        &options,
        output_dir,
        &mut TradeBuyStrategy::default(),
        &mut ApproveAll,
        &mut ImmediateFill,
    )
}

/// Run one backtest job with injected strategy, risk, and execution seams.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the engine rejects the run or the artifact
/// cannot be written.
pub fn run_job_with(
    configuration: &BacktestConfig,
    options: &BacktestOptions,
    output_dir: Option<&Path>,
    strategy: &mut dyn StrategyRuntime,
    risk: &mut dyn RiskModel,
    execution: &mut dyn ExecutionModel,
) -> Result<BacktestJobSummary, BacktestJobError> {
    let result = run_backtest(configuration, None, options, strategy, risk, execution)?;
    let directory = output_dir.map_or_else(
        || PathBuf::from(".local").join("backtests").join(&configuration.run_id),
        Path::to_path_buf,
    );
    let artifact_path = write_artifact(&directory, &configuration.run_id, &result)?;
    Ok(summarize(&result, artifact_path))
}

fn write_artifact(
    directory: &Path,
    run_id: &str,
    result: &BacktestResult,
) -> Result<PathBuf, BacktestJobError> {
    std::fs::create_dir_all(directory).map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let path = directory.join("backtest.json");
    let mut payload = result.artifact_json().into_bytes();
    payload.push(b'\n');
    // The run identifier is embedded in the manifest, so the written document
    // is self-describing and independent of the directory name.
    debug_assert_eq!(result.manifest.run_id, run_id);
    std::fs::write(&path, payload).map_err(|error| BacktestJobError::Io(error.to_string()))?;
    Ok(path)
}

fn summarize(result: &BacktestResult, artifact_path: PathBuf) -> BacktestJobSummary {
    BacktestJobSummary {
        run_id: result.manifest.run_id.clone(),
        status: result.status.value().to_string(),
        manifest_hash: result.manifest.manifest_hash(),
        artifact_digest: result.artifact_digest(),
        outcome_digest: result.outcome_digest(),
        market_events: result.market_event_count,
        orders: result.order_count,
        fills: result.fill_count,
        artifact_path,
        reason: result.reason.clone(),
    }
}

#[cfg(test)]
mod tests {
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
        assert_eq!(parse_shell("shell: backtest\n").as_deref(), Some("backtest"));
        assert_eq!(parse_shell("market_data:\n  shell: nested\n"), None);
        assert_eq!(parse_scalar("run_id: bt-1\n", "run_id").as_deref(), Some("bt-1"));
        assert_eq!(parse_scalar("\"seed\": 42\n", "seed").as_deref(), Some("42"));
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
    fn runs_end_to_end_and_writes_backtest_json() {
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
        let _ = std::fs::remove_dir_all(&directory);
    }
}
