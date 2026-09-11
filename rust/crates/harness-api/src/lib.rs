//! Clap-based command-line surface for the quant-harness Rust port.
//!
//! This crate mirrors the Typer command surface defined by the Python modules
//! `harness.cli` (`src/harness/cli/__init__.py`), `harness.cli.application`,
//! `harness.cli.backtest`, `harness.cli.backtest_job`, `harness.cli.api`,
//! `harness.cli.trader`, and `harness.mcp.__main__`:
//!
//! * `harness` — the process facade that groups `api`, `backtest-job`,
//!   `trader`, and `mcp` (root help is printed when no group is selected).
//! * `backtest-job` — the standalone entry point for one deterministic
//!   backtest job.
//!
//! Parity scope for this task:
//!
//! * Command names, help descriptions, flags, and exit codes match the Typer
//!   CLI for the facade and `backtest-job`.
//! * `api` and `mcp` are recognized and served by the Rust process
//!   implementations; `trader` is recognized so the facade help is identical,
//!   but its execution body arrives with its own port task and until then it
//!   reports that the Rust port is not yet available and exits `2`.
//! * The `backtest-job` command is served by the native Rust engine through
//!   [`NativeBacktestJobRunner`], which drives the configured Python strategies
//!   via the `harness-host` PyO3 adapter. [`BacktestJobRunner`] remains the
//!   seam so the command surface is stable and test doubles are possible;
//!   [`UnportedBacktestJobRunner`] is retained for the unported `--data-dir`
//!   fixture path.

use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::{Args, CommandFactory, Parser, Subcommand};

pub mod http;
pub mod mcp_cli;
pub mod native;

pub use native::NativeBacktestJobRunner;

/// Root help/`about` text for the `harness` facade.
///
/// Mirrors `typer.Typer(help="Run a quant-harness process from the shared FCA
/// runtime image.")` in `harness.cli.application`.
pub const HARNESS_ABOUT: &str = "Run a quant-harness process from the shared FCA runtime image.";

/// Process facade mirroring `harness.cli.application:app`.
///
/// Parsing with no subcommand leaves [`HarnessCli::command`] as `None`; the
/// caller then prints the root help, exactly like the Typer callback in
/// `harness.cli.application.show_help`.
#[derive(Debug, Parser)]
#[command(
    name = "harness",
    about = HARNESS_ABOUT,
    long_about = None,
    disable_help_subcommand = true,
    subcommand_required = false,
    arg_required_else_help = false
)]
pub struct HarnessCli {
    /// The selected process group, or `None` for the bare `harness` invocation.
    #[command(subcommand)]
    pub command: Option<HarnessCommand>,
}

/// Process groups registered by the Python facade.
///
/// The order matches `harness.cli.application` plus the `mcp` group that
/// `harness.cli.__init__` adds.
#[derive(Debug, Subcommand)]
pub enum HarnessCommand {
    /// Serve quant-harness results and job operations.
    #[command(name = "api")]
    Api(ApiArgs),
    /// Run one deterministic backtest job.
    #[command(name = "backtest-job")]
    BacktestJob(BacktestJobArgs),
    /// Run one supervised paper or shadow trading session to completion.
    #[command(name = "trader")]
    Trader(TraderArgs),
    /// Serve the harness MCP server over stdio.
    #[command(name = "mcp")]
    Mcp(McpArgs),
}

/// Options for `harness backtest-job` / the `backtest-job` entry point.
///
/// Mirrors the Typer callback in `harness.cli.backtest.run_job`.
#[derive(Debug, Clone, Default, Args)]
pub struct BacktestJobArgs {
    /// Path to a typed backtest configuration document.
    #[arg(long, value_name = "PATH")]
    pub config: Option<PathBuf>,

    /// Optional local Parquet lake directory
    /// (date=/asset_class=/data_type=/SYMBOL.parquet). Omit for the
    /// deterministic synthetic benchmark fixture.
    #[arg(long = "data-dir", value_name = "PATH")]
    pub data_dir: Option<PathBuf>,

    /// Directory for the canonical run artifact document. Defaults to
    /// `.local/backtests/<run_id>`.
    #[arg(long = "output-dir", value_name = "PATH")]
    pub output_dir: Option<PathBuf>,
}

/// Options for `harness api`, mirroring `harness.cli.api.serve`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Args)]
pub struct ApiArgs {
    /// Path to a typed API configuration document.
    #[arg(long, value_name = "PATH")]
    pub config: Option<PathBuf>,

    /// Override the configured bind host.
    #[arg(long, value_name = "HOST")]
    pub host: Option<String>,

    /// Override the configured bind port.
    #[arg(long, value_name = "PORT")]
    pub port: Option<u16>,
}

/// Options for `harness trader`, mirroring `harness.cli.trader.launch`.
#[derive(Debug, Clone, Args)]
pub struct TraderArgs {
    /// Path to the trader configuration document.
    #[arg(
        long,
        value_name = "PATH",
        default_value = "config/harness/trader.yaml"
    )]
    pub config: PathBuf,

    /// Optional `status` subcommand.
    #[command(subcommand)]
    pub command: Option<TraderCommand>,
}

/// Subcommands of `harness trader`.
#[derive(Debug, Clone, Subcommand)]
pub enum TraderCommand {
    /// Print the latest supervision document for the configured run.
    #[command(name = "status")]
    Status(TraderStatusArgs),
}

/// Options for `harness trader status`.
#[derive(Debug, Clone, Args)]
pub struct TraderStatusArgs {
    /// Path to the trader configuration document.
    #[arg(
        long,
        value_name = "PATH",
        default_value = "config/harness/trader.yaml"
    )]
    pub config: PathBuf,
}

/// Options for `harness mcp`, mirroring `harness.mcp.__main__.serve`.
#[derive(Debug, Clone, PartialEq, Eq, Args)]
pub struct McpArgs {
    /// `run_backtest` boundary: 'local' process or 'remote' API launch.
    #[arg(long, value_name = "MODE", default_value = "local")]
    pub mode: String,

    /// Remote harness API base URL (defaults to `HARNESS_API_BASE_URL`).
    #[arg(long = "api-base-url", value_name = "URL")]
    pub api_base_url: Option<String>,

    /// Expose the capability-gated `launch_trader` tool.
    #[arg(long = "enable-trader", action = clap::ArgAction::SetTrue)]
    pub enable_trader: bool,
}

/// Standalone `backtest-job` entry point mirroring
/// `harness.cli.backtest_job.main`.
#[derive(Debug, Parser)]
#[command(
    name = "backtest-job",
    about = "Run one deterministic backtest job.",
    disable_help_subcommand = true
)]
pub struct BacktestJobCli {
    /// The shared backtest job options.
    #[command(flatten)]
    pub args: BacktestJobArgs,
}

/// The canonical summary the Python `backtest-job` prints after a run.
///
/// Field order matches the `typer.echo` sequence in
/// `harness.cli.backtest.run_job`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BacktestRunSummary {
    /// Run identifier from the configuration document.
    pub run_id: String,
    /// Terminal status (`completed` for a successful run).
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
    /// Failure reason, present when `status` is not `completed`.
    pub reason: Option<String>,
}

impl BacktestRunSummary {
    /// Render the summary exactly as the Typer CLI writes it to stdout.
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

/// Failures a [`BacktestJobRunner`] can report to the CLI.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BacktestJobError {
    /// The Rust backtest engine has not been ported yet.
    EngineNotPorted,
    /// The engine rejected the run for a domain reason.
    Engine(String),
}

impl fmt::Display for BacktestJobError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EngineNotPorted => formatter.write_str(
                "the Rust backtest engine is not yet ported; the Python harness \
                 remains authoritative",
            ),
            Self::Engine(reason) => formatter.write_str(reason),
        }
    }
}

impl std::error::Error for BacktestJobError {}

/// Execution seam between the parsed `backtest-job` surface and the engine.
///
/// The ported Rust backtest pipeline implements this trait; until then the
/// CLI is fully exercised through [`UnportedBacktestJobRunner`] and test
/// doubles, so the command surface is stable while the engine lands.
pub trait BacktestJobRunner {
    /// Run one backtest for the already-validated command arguments.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestJobError`] when the engine cannot complete the run.
    fn run(&self, args: &BacktestJobArgs) -> Result<BacktestRunSummary, BacktestJobError>;
}

/// Default runner used by the binaries until the Rust engine is ported.
#[derive(Debug, Clone, Copy, Default)]
pub struct UnportedBacktestJobRunner;

impl BacktestJobRunner for UnportedBacktestJobRunner {
    fn run(&self, _args: &BacktestJobArgs) -> Result<BacktestRunSummary, BacktestJobError> {
        Err(BacktestJobError::EngineNotPorted)
    }
}

/// The channel and exit code of one command invocation.
///
/// Encodes the Typer behavior precisely: help and success write stdout and
/// exit `0`; usage errors write stderr and exit `2`; engine failures write the
/// captured summary to stdout, the reason to stderr, and exit `1`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommandResult {
    /// Print `stdout` (help text) and exit `0`.
    Help(String),
    /// Print `stdout` (if any) and exit `0`.
    Completed {
        /// Text written to stdout.
        stdout: String,
    },
    /// Print the message to stderr and exit `2`.
    UsageError(String),
    /// Print `stdout` to stdout, `stderr` to stderr, and exit `1`.
    Failed {
        /// Text written to stdout before the failure.
        stdout: String,
        /// Failure reason written to stderr.
        stderr: String,
    },
    /// Run the axum HTTP server with these options (handled by the binary).
    ServeApi(ApiArgs),
    /// Run the MCP stdio server with these options (handled by the binary).
    ServeMcp(McpArgs),
}

impl CommandResult {
    /// The process exit code this result maps to.
    ///
    /// The serve variants are dispatched by the binary rather than emitted and
    /// therefore report success.
    #[must_use]
    pub fn exit_code(&self) -> u8 {
        match self {
            Self::Help(_) | Self::Completed { .. } | Self::ServeApi(_) | Self::ServeMcp(_) => 0,
            Self::Failed { .. } => 1,
            Self::UsageError(_) => 2,
        }
    }
}

/// Render the facade help exactly as the root `harness` invocation shows it.
#[must_use]
pub fn root_help() -> String {
    HarnessCli::command().render_help().to_string()
}

/// Dispatch a parsed facade invocation to its process group.
///
/// `runner` is the injected backtest engine seam; the `api` and `mcp` groups
/// return their serve requests for the binary to execute, and `trader`
/// reports that its Rust port is pending.
#[must_use]
pub fn run(cli: HarnessCli, runner: &dyn BacktestJobRunner) -> CommandResult {
    match cli.command {
        None => CommandResult::Help(root_help()),
        Some(HarnessCommand::BacktestJob(args)) => execute_backtest_job(&args, runner),
        Some(HarnessCommand::Api(args)) => CommandResult::ServeApi(args),
        Some(HarnessCommand::Trader(_)) => not_yet_ported("trader"),
        Some(HarnessCommand::Mcp(args)) => CommandResult::ServeMcp(args),
    }
}

/// Dispatch the standalone `backtest-job` entry point.
#[must_use]
pub fn run_backtest_job(cli: BacktestJobCli, runner: &dyn BacktestJobRunner) -> CommandResult {
    execute_backtest_job(&cli.args, runner)
}

/// Validate and execute one backtest job, matching `harness.cli.backtest`.
///
/// Mirrors the Python control flow: a missing `--config` and a configuration
/// whose `shell` is not `backtest` both exit `2` with a stderr message, and an
/// engine failure exits `1` after printing the summary.
#[must_use]
pub fn execute_backtest_job(
    args: &BacktestJobArgs,
    runner: &dyn BacktestJobRunner,
) -> CommandResult {
    let Some(config) = args.config.as_deref() else {
        return CommandResult::UsageError(
            "A backtest configuration is required: backtest-job --config <path>".to_string(),
        );
    };

    let shell = match configuration_shell(config) {
        Ok(shell) => shell,
        Err(error) => {
            return CommandResult::UsageError(format!(
                "configuration could not be loaded: {error}"
            ));
        }
    };
    if shell != "backtest" {
        return CommandResult::UsageError(format!(
            "configuration shell must be 'backtest', not '{shell}'"
        ));
    }

    match runner.run(args) {
        Ok(summary) => {
            let stdout = summary.stdout_lines().join("\n");
            if summary.status != "completed" {
                let reason = summary.reason.unwrap_or_else(|| summary.status.clone());
                return CommandResult::Failed {
                    stdout,
                    stderr: format!("reason: {reason}"),
                };
            }
            CommandResult::Completed { stdout }
        }
        Err(error) => CommandResult::Failed {
            stdout: String::new(),
            stderr: format!("Backtest failed: {error}"),
        },
    }
}

/// Emit a [`CommandResult`] to the process streams and return its exit code.
#[must_use]
pub fn emit(result: CommandResult) -> ExitCode {
    match result {
        CommandResult::Help(text) | CommandResult::Completed { stdout: text } => {
            if !text.is_empty() {
                println!("{text}");
            }
            ExitCode::SUCCESS
        }
        CommandResult::UsageError(message) => {
            eprintln!("{message}");
            ExitCode::from(2)
        }
        CommandResult::Failed { stdout, stderr } => {
            if !stdout.is_empty() {
                println!("{stdout}");
            }
            eprintln!("{stderr}");
            ExitCode::from(1)
        }
        CommandResult::ServeApi(_) | CommandResult::ServeMcp(_) => {
            eprintln!("an internal error occurred while dispatching a server command");
            ExitCode::from(2)
        }
    }
}

fn not_yet_ported(group: &str) -> CommandResult {
    CommandResult::UsageError(format!(
        "the '{group}' command is not yet ported to the Rust CLI; use the Python \
         entrypoint until its port task lands"
    ))
}

/// Errors surfaced while reading a configuration document's `shell`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfigurationError {
    /// The document could not be read.
    Io(String),
    /// The document has no top-level `shell` key.
    MissingShell,
}

impl fmt::Display for ConfigurationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(message) => write!(formatter, "{message}"),
            Self::MissingShell => formatter.write_str("configuration has no 'shell' key"),
        }
    }
}

impl std::error::Error for ConfigurationError {}

/// Read the top-level `shell` scalar from a configuration document.
///
/// This is a minimal stand-in for the typed loader in
/// `harness.configuration.loader`; it only needs to distinguish the backtest
/// shell from the other shells so the CLI can reproduce the Python usage
/// errors. The typed validation remains owned by the Python engine until the
/// configuration port lands.
///
/// # Errors
///
/// Returns [`ConfigurationError::Io`] when the file cannot be read, or
/// [`ConfigurationError::MissingShell`] when no top-level `shell` key exists.
pub fn configuration_shell(path: &Path) -> Result<String, ConfigurationError> {
    let text =
        fs::read_to_string(path).map_err(|error| ConfigurationError::Io(error.to_string()))?;
    parse_configuration_shell(&text).ok_or(ConfigurationError::MissingShell)
}

/// Extract a top-level `shell: <value>` (YAML) or `"shell": "<value>"` (JSON).
#[must_use]
pub fn parse_configuration_shell(text: &str) -> Option<String> {
    for line in text.lines() {
        let trimmed = line.trim_end();
        if trimmed.trim_start().len() != trimmed.len() {
            continue;
        }
        let rest = match trimmed
            .strip_prefix("shell:")
            .or_else(|| trimmed.strip_prefix("\"shell\":"))
        {
            Some(rest) => rest,
            None => continue,
        };
        let value = rest
            .trim()
            .trim_matches(|character| character == '"' || character == '\'');
        if !value.is_empty() {
            return Some(value.to_string());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

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
        assert!(help.contains("trader"));
    }

    #[test]
    fn bare_harness_shows_root_help() {
        let cli = HarnessCli::try_parse_from(["harness"]).expect("parse");
        let result = run(cli, &UnportedBacktestJobRunner);
        match result {
            CommandResult::Help(help) => assert!(help.contains("backtest-job")),
            other => panic!("expected help, got {other:?}"),
        }
    }

    #[test]
    fn backtest_job_parses_every_flag() {
        let cli = HarnessCli::try_parse_from([
            "harness",
            "backtest-job",
            "--config",
            "backtest.yaml",
            "--data-dir",
            "lake",
            "--output-dir",
            "out",
        ])
        .expect("parse");
        let Some(HarnessCommand::BacktestJob(args)) = cli.command else {
            panic!("expected backtest-job");
        };
        assert_eq!(args.config, Some(PathBuf::from("backtest.yaml")));
        assert_eq!(args.data_dir, Some(PathBuf::from("lake")));
        assert_eq!(args.output_dir, Some(PathBuf::from("out")));
    }

    #[test]
    fn backtest_job_requires_a_configuration() {
        let cli = HarnessCli::try_parse_from(["harness", "backtest-job"]).expect("parse");
        let result = run(cli, &UnportedBacktestJobRunner);
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
        let result = run(cli, &UnportedBacktestJobRunner);
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
        let result = run(cli, &runner);
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
        let result = run(cli, &UnportedBacktestJobRunner);
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
        assert!(help.contains("--data-dir"));
        assert!(help.contains("--output-dir"));
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
    fn pending_groups_report_not_yet_ported() {
        let cli = HarnessCli::try_parse_from(["harness", "trader"]).expect("parse");
        match run(cli, &UnportedBacktestJobRunner) {
            CommandResult::UsageError(message) => {
                assert!(message.contains("trader"), "missing group in {message}");
            }
            other => panic!("expected usage error for trader, got {other:?}"),
        }
    }

    #[test]
    fn api_and_mcp_dispatch_to_their_servers() {
        let api = HarnessCli::try_parse_from(["harness", "api"]).expect("parse");
        match run(api, &UnportedBacktestJobRunner) {
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
        match run(mcp, &UnportedBacktestJobRunner) {
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
}
