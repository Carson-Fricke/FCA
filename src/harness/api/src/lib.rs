//! Clap-based command-line surface for the quant-harness Rust port.
//!
//! This crate mirrors the Typer command surface defined by the Python modules
//! `harness.cli` (`src/harness/cli/__init__.py`), `harness.cli.application`,
//! `harness.cli.backtest`, `harness.cli.backtest_job`, `harness.cli.api`,
//! `harness.cli.trader`, and `harness.mcp.__main__`:
//!
//! * `harness` — the process facade that groups `api`, `backtest-job`,
//!   `study-job`, `screen-job`, `trader`, and `mcp` (root help is printed when
//!   no group is selected).
//! * `backtest-job` — the standalone entry point for one deterministic
//!   backtest job.
//! * `study-job` — the standalone entry point for one deterministic study
//!   job.
//! * `screen-job` — the standalone entry point for one deterministic
//!   screening job (`shell: screen`), writing `screen.json`.
//!
//! Parity scope for this task:
//!
//! * Command names, help descriptions, flags, and exit codes match the Typer
//!   CLI for the facade and `backtest-job`.
//! * `api`, `mcp`, and `trader` are recognized and served by the Rust process
//!   implementations; `trader` runs the supervised paper/shadow runner in the
//!   `live` crate.
//! * The `backtest-job` command is served by the native Rust engine through
//!   [`NativeBacktestJobRunner`], which drives the configured JIT-authored
//!   Rust programs via the `harness-strategy-jit` seam. [`BacktestJobRunner`]
//!   remains the seam so the command surface is stable and test doubles are
//!   possible. Market data streams from the market-lake HTTP routes through
//!   `harness-lakeclient`; the endpoint resolves from `LAKE_API_URL` /
//!   `LAKE_API_BASE_URL` and `LAKE_API__BEARER_TOKEN`. There is no local data
//!   directory surface.

use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::{Args, CommandFactory, Parser, Subcommand};

pub mod http;
pub mod local;
pub mod mcp_cli;
pub mod native;

pub use native::{NativeBacktestJobRunner, NativeScreenJobRunner, NativeStudyJobRunner};

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
    /// Run one deterministic study job (replay for indicators and models).
    #[command(name = "study-job")]
    StudyJob(StudyJobArgs),
    /// Run one deterministic screening job (one cross-sectional evaluation).
    #[command(name = "screen-job")]
    ScreenJob(ScreenJobArgs),
    /// Run one supervised paper or shadow trading session to completion.
    #[command(name = "trader")]
    Trader(TraderArgs),
    /// Serve the harness MCP server over stdio.
    #[command(name = "mcp")]
    Mcp(McpArgs),
}

/// Options for `harness backtest-job` / the `backtest-job` entry point.
///
/// Mirrors the Typer callback in `harness.cli.backtest.run_job`. Market data
/// streams from the market-lake routes configured by `LAKE_API_URL` /
/// `LAKE_API_BASE_URL` and `LAKE_API__BEARER_TOKEN`.
#[derive(Debug, Clone, Default, Args)]
pub struct BacktestJobArgs {
    /// Path to a typed backtest configuration document.
    #[arg(long, value_name = "PATH")]
    pub config: Option<PathBuf>,

    /// Directory for the canonical run artifact document. Defaults to
    /// `.local/backtests/<run_id>`.
    #[arg(long = "output-dir", value_name = "PATH")]
    pub output_dir: Option<PathBuf>,
}

/// Options for `harness study-job` / the `study-job` entry point.
///
/// A study reuses the replay window and data options but runs platform study
/// programs with no order execution and writes a `study.json` artifact. The
/// lake endpoint comes from `LAKE_API_URL` / `LAKE_API_BASE_URL` and
/// `LAKE_API__BEARER_TOKEN`.
#[derive(Debug, Clone, Default, Args)]
pub struct StudyJobArgs {
    /// Path to a typed study configuration document.
    #[arg(long, value_name = "PATH")]
    pub config: Option<PathBuf>,

    /// Directory for the canonical study artifact document. Defaults to
    /// `.local/studies/<run_id>`.
    #[arg(long = "output-dir", value_name = "PATH")]
    pub output_dir: Option<PathBuf>,
}

/// Options for `harness screen-job` / the `screen-job` entry point.
///
/// A screening run evaluates one authored Rust program once over an as-of
/// cross-section and writes a `screen.json` artifact. Market data arrives only
/// through the injected data-source port; the CLI composes the fail-loud
/// placeholder until the lake-route adapter lands.
#[derive(Debug, Clone, Default, PartialEq, Eq, Args)]
pub struct ScreenJobArgs {
    /// Path to a typed screening configuration document (`shell: screen`).
    #[arg(long, value_name = "PATH")]
    pub config: Option<PathBuf>,

    /// Directory for the canonical `screen.json` artifact. Defaults to
    /// `.local/screens/<run_id>`.
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
#[derive(Debug, Clone, PartialEq, Eq, Args)]
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
#[derive(Debug, Clone, PartialEq, Eq, Subcommand)]
pub enum TraderCommand {
    /// Print the latest supervision document for the configured run.
    #[command(name = "status")]
    Status(TraderStatusArgs),
}

/// Options for `harness trader status`.
#[derive(Debug, Clone, PartialEq, Eq, Args)]
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

/// Standalone `study-job` entry point.
#[derive(Debug, Parser)]
#[command(
    name = "study-job",
    about = "Run one deterministic study job (replay for indicators and models).",
    disable_help_subcommand = true
)]
pub struct StudyJobCli {
    /// The shared study job options.
    #[command(flatten)]
    pub args: StudyJobArgs,
}

/// Standalone `screen-job` entry point.
#[derive(Debug, Parser)]
#[command(
    name = "screen-job",
    about = "Run one deterministic screening job (one cross-sectional evaluation).",
    disable_help_subcommand = true
)]
pub struct ScreenJobCli {
    /// The shared screening job options.
    #[command(flatten)]
    pub args: ScreenJobArgs,
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

/// Always-failing runner double used to test CLI error handling.
#[derive(Debug, Clone, Copy, Default)]
pub struct UnportedBacktestJobRunner;

impl BacktestJobRunner for UnportedBacktestJobRunner {
    fn run(&self, _args: &BacktestJobArgs) -> Result<BacktestRunSummary, BacktestJobError> {
        Err(BacktestJobError::EngineNotPorted)
    }
}

/// Execution seam between the parsed `study-job` surface and the study engine.
///
/// The native study path implements this trait; a study runs platform study
/// programs with no order execution and writes a `study.json` artifact.
pub trait StudyJobRunner {
    /// Run one study for the already-validated command arguments.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestJobError`] when the study cannot complete the run.
    fn run(&self, args: &StudyJobArgs) -> Result<BacktestRunSummary, BacktestJobError>;
}

/// Always-failing study runner double used to test CLI error handling.
#[derive(Debug, Clone, Copy, Default)]
pub struct UnportedStudyJobRunner;

impl StudyJobRunner for UnportedStudyJobRunner {
    fn run(&self, _args: &StudyJobArgs) -> Result<BacktestRunSummary, BacktestJobError> {
        Err(BacktestJobError::EngineNotPorted)
    }
}

/// The canonical summary the `screen-job` surface prints after a run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScreenRunSummary {
    /// Deterministic run identifier derived from the configuration content.
    pub run_id: String,
    /// Terminal status (`complete` or `failed`).
    pub status: String,
    /// Number of ranked rows.
    pub rows: u64,
    /// Path of the written `screen.json` artifact.
    pub artifact_path: PathBuf,
    /// Failure reason, present when the run did not complete.
    pub reason: Option<String>,
}

impl ScreenRunSummary {
    /// Render the summary exactly as the CLI writes it to stdout.
    #[must_use]
    pub fn stdout_lines(&self) -> Vec<String> {
        vec![
            format!("run_id: {}", self.run_id),
            format!("status: {}", self.status),
            format!("rows: {}", self.rows),
            format!("artifacts: {}", self.artifact_path.display()),
        ]
    }
}

/// Failures a [`ScreenJobRunner`] can report to the CLI.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScreenJobError {
    /// The runner rejected the run before an artifact could be written.
    Engine(String),
}

impl fmt::Display for ScreenJobError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Engine(reason) => formatter.write_str(reason),
        }
    }
}

impl std::error::Error for ScreenJobError {}

/// Execution seam between the parsed `screen-job` surface and the screening
/// run path.
pub trait ScreenJobRunner {
    /// Run one screening configuration for the already-validated arguments.
    ///
    /// # Errors
    ///
    /// Returns [`ScreenJobError`] when no run artifact could be produced.
    fn run(&self, args: &ScreenJobArgs) -> Result<ScreenRunSummary, ScreenJobError>;
}

/// Always-failing screening runner double used to test CLI error handling.
#[derive(Debug, Clone, Copy, Default)]
pub struct UnavailableScreenJobRunner;

impl ScreenJobRunner for UnavailableScreenJobRunner {
    fn run(&self, _args: &ScreenJobArgs) -> Result<ScreenRunSummary, ScreenJobError> {
        Err(ScreenJobError::Engine(
            "the screening runner is not composed for this process".to_string(),
        ))
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
    /// Run the supervised trader process (handled by the binary).
    RunTrader(TraderArgs),
}

impl CommandResult {
    /// The process exit code this result maps to.
    ///
    /// The serve variants are dispatched by the binary rather than emitted and
    /// therefore report success.
    #[must_use]
    pub fn exit_code(&self) -> u8 {
        match self {
            Self::Help(_)
            | Self::Completed { .. }
            | Self::ServeApi(_)
            | Self::ServeMcp(_)
            | Self::RunTrader(_) => 0,
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
/// `runner`, `study_runner`, and `screen_runner` are the injected engine
/// seams; the `api`, `mcp`, and `trader` groups return their process requests
/// for the binary to execute.
#[must_use]
pub fn run(
    cli: HarnessCli,
    runner: &dyn BacktestJobRunner,
    study_runner: &dyn StudyJobRunner,
    screen_runner: &dyn ScreenJobRunner,
) -> CommandResult {
    match cli.command {
        None => CommandResult::Help(root_help()),
        Some(HarnessCommand::BacktestJob(args)) => execute_backtest_job(&args, runner),
        Some(HarnessCommand::StudyJob(args)) => execute_study_job(&args, study_runner),
        Some(HarnessCommand::ScreenJob(args)) => execute_screen_job(&args, screen_runner),
        Some(HarnessCommand::Api(args)) => CommandResult::ServeApi(args),
        Some(HarnessCommand::Trader(args)) => CommandResult::RunTrader(args),
        Some(HarnessCommand::Mcp(args)) => CommandResult::ServeMcp(args),
    }
}

/// Dispatch the standalone `study-job` entry point.
#[must_use]
pub fn run_study_job(cli: StudyJobCli, runner: &dyn StudyJobRunner) -> CommandResult {
    execute_study_job(&cli.args, runner)
}

/// Dispatch the standalone `screen-job` entry point.
#[must_use]
pub fn run_screen_job(cli: ScreenJobCli, runner: &dyn ScreenJobRunner) -> CommandResult {
    execute_screen_job(&cli.args, runner)
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

/// Validate and execute one study job.
///
/// A missing `--config` and a configuration whose `shell` is not `study` both
/// exit `2`; an engine failure exits `1` after printing the summary.
#[must_use]
pub fn execute_study_job(args: &StudyJobArgs, runner: &dyn StudyJobRunner) -> CommandResult {
    let Some(config) = args.config.as_deref() else {
        return CommandResult::UsageError(
            "A study configuration is required: study-job --config <path>".to_string(),
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
    if shell != "study" {
        return CommandResult::UsageError(format!(
            "configuration shell must be 'study', not '{shell}'"
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
            stderr: format!("Study failed: {error}"),
        },
    }
}

/// Validate and execute one screening job.
///
/// A missing `--config` and a configuration whose `shell` is not `screen` both
/// exit `2`; a failed screening run prints its summary and exits `1` after the
/// runner records the failed `screen.json` artifact.
#[must_use]
pub fn execute_screen_job(args: &ScreenJobArgs, runner: &dyn ScreenJobRunner) -> CommandResult {
    let Some(config) = args.config.as_deref() else {
        return CommandResult::UsageError(
            "A screening configuration is required: screen-job --config <path>".to_string(),
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
    if shell != "screen" {
        return CommandResult::UsageError(format!(
            "configuration shell must be 'screen', not '{shell}'"
        ));
    }

    match runner.run(args) {
        Ok(summary) => {
            let stdout = summary.stdout_lines().join("\n");
            if summary.status != "complete" {
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
            stderr: format!("Screening failed: {error}"),
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
        CommandResult::ServeApi(_) | CommandResult::ServeMcp(_) | CommandResult::RunTrader(_) => {
            eprintln!("an internal error occurred while dispatching a subprocess command");
            ExitCode::from(2)
        }
    }
}

/// Run the supervised trader process described by a parsed `harness trader`.
///
/// This is the process body for the facade group; the standalone `trader`
/// binary serves the same composition through the `live` crate.
#[must_use]
pub fn run_trader(args: &TraderArgs) -> ExitCode {
    match &args.command {
        Some(TraderCommand::Status(status)) => {
            match live::supervision::read_supervision_status(status.config.as_path()) {
                Some(payload) => {
                    println!("{payload}");
                    ExitCode::SUCCESS
                }
                None => {
                    eprintln!("no supervision record for {}", status.config.display());
                    ExitCode::from(1)
                }
            }
        }
        None => ExitCode::from(live::supervision::run_trader_process(&args.config).code()),
    }
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
