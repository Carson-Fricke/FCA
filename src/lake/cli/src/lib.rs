//! The `lake` command surface: the top-level API process and the local
//! `api`, `ingest`, and `reset` commands.
//!
//! The command implementation lives here rather than in `main.rs` so
//! integration tests can drive the parser and the command bodies directly. The
//! binary is a thin wrapper: [`run`] reads the process environment, executes
//! the command, writes the captured streams, and returns the exit code.
//!
//! Failure wording matches the Python entrypoints: a local command prints
//! `lake local: error: <message>` and the top-level API prints
//! `lake: error: <message>`, both to stderr with exit code 2. Argument-parsing
//! failures keep clap's exit code 2 and help output exits 0.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::Router;
use clap::{Parser, Subcommand};
use lake_configuration::{LakeSettings, UniverseSettings};
use lake_contracts::{Date, DateTime};
use lake_hosted::{api::create_api_router, HostedEnsureError};
use lake_http::ApiClock;
use lake_local::{create_local_app, run_local_ingest, LocalIngestOptions, LocalLayout};

/// The TCP port both `api` commands bind when `--port` is absent.
const DEFAULT_PORT: u16 = 8000;

/// The environment variable carrying the static bearer token.
const BEARER_TOKEN_ENV: &str = "LAKE_API__BEARER_TOKEN";

/// The exit code for every command failure and clap usage error.
const FAILURE_CODE: i32 = 2;

/// The captured result of one command invocation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CliOutcome {
    /// Process exit code.
    pub code: i32,
    /// Text written to stdout; empty when nothing was written.
    pub stdout: String,
    /// Text written to stderr; empty when nothing was written.
    pub stderr: String,
}

/// Parse and run one command line, returning the process exit code.
///
/// `argv` excludes the binary name, exactly as the Python `lake` entrypoint
/// receives `sys.argv[1:]`; a leading `lake`/`lake.exe` element is tolerated so
/// a full `std::env::args()` vector also parses.
pub fn run(argv: Vec<String>) -> i32 {
    let environ: BTreeMap<String, String> = std::env::vars().collect();
    let outcome = execute(&argv, &environ);
    print!("{}", outcome.stdout);
    eprint!("{}", outcome.stderr);
    outcome.code
}

/// Execute one command line against an injected environment.
///
/// This is the testable seam behind [`run`]: settings, layout, bearer, and
/// provider resolution all read the supplied map rather than the process
/// environment, and output is captured instead of written.
pub fn execute(argv: &[String], environ: &BTreeMap<String, String>) -> CliOutcome {
    let command = match parse(argv) {
        Ok(command) => command,
        Err(outcome) => return outcome,
    };
    match command {
        Command::Api { host, port } => run_api(&host, port, environ),
        Command::Local { command } => match command {
            LocalCommand::Api {
                host,
                port,
                data_dir,
            } => run_local_api(&host, port, data_dir.as_deref(), environ),
            LocalCommand::Ingest {
                symbol,
                session_date,
                data_dir,
            } => run_local_ingest_command(symbol, session_date, data_dir.as_deref(), environ),
            LocalCommand::Reset { yes, data_dir } => {
                run_local_reset(yes, data_dir.as_deref(), environ)
            }
        },
    }
}

/// Build the published API with Azure storage when configured.
///
/// Hosted composition owns durable catalog/jobs, authenticated reads, and
/// real ingestion. The CLI never constructs an independent in-memory router.
pub fn build_api_router(environ: &BTreeMap<String, String>) -> Result<Router, HostedEnsureError> {
    create_api_router(environ, Arc::new(system_now))
}

/// The command tree published by the binary.
#[derive(Debug, Parser)]
#[command(
    name = "lake",
    about = "Run a market-lake process from the shared runtime image.",
    disable_help_subcommand = true
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

/// Top-level market-lake processes.
#[derive(Debug, Subcommand)]
enum Command {
    /// Run the market-lake API process.
    Api {
        /// Address the API binds.
        #[arg(long, default_value = "0.0.0.0")]
        host: String,
        /// TCP port the API binds, between 1 and 65535.
        #[arg(long, default_value_t = DEFAULT_PORT, value_parser = clap::value_parser!(u16).range(1..))]
        port: u16,
    },
    /// Run the market lake with local files and no Azure resources.
    Local {
        #[command(subcommand)]
        command: LocalCommand,
    },
}

/// Local process and state commands.
#[derive(Debug, Subcommand)]
enum LocalCommand {
    /// Serve the API over local state.
    Api {
        /// Address the API binds.
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        /// TCP port the API binds, between 1 and 65535.
        #[arg(long, default_value_t = DEFAULT_PORT, value_parser = clap::value_parser!(u16).range(1..))]
        port: u16,
        /// Local state root; defaults to LAKE_LOCAL_DATA_DIR or .local/market-lake.
        #[arg(long, value_name = "DIR")]
        data_dir: Option<PathBuf>,
    },
    /// Fetch one closed Alpaca equity session.
    Ingest {
        /// Configured equity symbol; defaults to the first soak symbol.
        #[arg(long)]
        symbol: Option<String>,
        /// Closed XNYS session date; defaults to the latest closed session.
        #[arg(long, value_parser = parse_session_date)]
        session_date: Option<Date>,
        /// Local state root; defaults to LAKE_LOCAL_DATA_DIR or .local/market-lake.
        #[arg(long, value_name = "DIR")]
        data_dir: Option<PathBuf>,
    },
    /// Delete generated local state.
    Reset {
        /// Confirm deletion without prompting.
        #[arg(long)]
        yes: bool,
        /// Local state root; defaults to LAKE_LOCAL_DATA_DIR or .local/market-lake.
        #[arg(long, value_name = "DIR")]
        data_dir: Option<PathBuf>,
    },
}

/// Parse an argv slice against the command tree, prepending the binary name.
fn parse(argv: &[String]) -> Result<Command, CliOutcome> {
    let mut arguments: Vec<String> = Vec::with_capacity(argv.len() + 1);
    arguments.push("lake".to_string());
    arguments.extend(trim_binary_name(argv).iter().cloned());
    match Cli::try_parse_from(arguments) {
        Ok(cli) => Ok(cli.command),
        Err(error) => Err(clap_outcome(error)),
    }
}

/// Drop a leading binary-name element so full `std::env::args()` vectors parse.
fn trim_binary_name(argv: &[String]) -> &[String] {
    match argv.first().map(String::as_str) {
        Some("lake") | Some("lake.exe") | Some("lake-cli") | Some("lake-cli.exe") => &argv[1..],
        _ => argv,
    }
}

/// Convert a clap parse result into the captured outcome and clap exit code.
fn clap_outcome(error: clap::Error) -> CliOutcome {
    let code = error.exit_code();
    let rendered = error.render().to_string();
    if error.use_stderr() {
        CliOutcome {
            code,
            stdout: String::new(),
            stderr: rendered,
        }
    } else {
        CliOutcome {
            code,
            stdout: rendered,
            stderr: String::new(),
        }
    }
}

/// Run the top-level API process.
fn run_api(host: &str, port: u16, environ: &BTreeMap<String, String>) -> CliOutcome {
    let router = match build_api_router(environ) {
        Ok(router) => router,
        Err(error) => return lake_failure(error),
    };
    match serve(router, host, port) {
        Ok(()) => success(String::new()),
        Err(error) => lake_failure(error),
    }
}

/// Serve the published API over local state without any Azure client.
fn run_local_api(
    host: &str,
    port: u16,
    data_dir: Option<&Path>,
    environ: &BTreeMap<String, String>,
) -> CliOutcome {
    let layout = match LocalLayout::from_environment(data_dir, environ) {
        Ok(layout) => layout,
        Err(error) => return local_failure(error),
    };
    if environment_value(environ, BEARER_TOKEN_ENV).is_none_or(|token| token.trim().is_empty()) {
        return local_failure(format!("{BEARER_TOKEN_ENV} is required for the local API"));
    }
    let now: ApiClock = Arc::new(system_now);
    let router = match create_local_app(&layout, environ, now) {
        Ok(router) => router,
        Err(error) => return local_failure(error),
    };
    match serve(router, host, port) {
        Ok(()) => success(String::new()),
        Err(error) => local_failure(error),
    }
}

/// Fetch one closed equity session and print the compact result JSON.
fn run_local_ingest_command(
    symbol: Option<String>,
    session_date: Option<Date>,
    data_dir: Option<&Path>,
    environ: &BTreeMap<String, String>,
) -> CliOutcome {
    let layout = match LocalLayout::from_environment(data_dir, environ) {
        Ok(layout) => layout,
        Err(error) => return local_failure(error),
    };
    let settings = match LakeSettings::load(None, environ) {
        Ok(settings) => settings,
        Err(error) => return local_failure(error),
    };
    let universes = match UniverseSettings::load(None, environ) {
        Ok(universes) => universes,
        Err(error) => return local_failure(error),
    };
    let options = LocalIngestOptions {
        layout,
        symbol,
        session_date,
        environ: environ.clone(),
        now: system_now(),
        universes,
        settings,
        provider: None,
        catalog: None,
    };
    let runtime = match tokio::runtime::Runtime::new() {
        Ok(runtime) => runtime,
        Err(error) => return local_failure(error),
    };
    match runtime.block_on(run_local_ingest(options)) {
        Ok(result) => match serde_json::to_string(&result) {
            Ok(json) => success(format!("{json}\n")),
            Err(error) => local_failure(error),
        },
        Err(error) => local_failure(error),
    }
}

/// Remove an initialized local tree and print the compact result JSON.
fn run_local_reset(
    yes: bool,
    data_dir: Option<&Path>,
    environ: &BTreeMap<String, String>,
) -> CliOutcome {
    if !yes {
        return local_failure("reset requires --yes");
    }
    let layout = match LocalLayout::from_environment(data_dir, environ) {
        Ok(layout) => layout,
        Err(error) => return local_failure(error),
    };
    if let Err(error) = layout.reset() {
        return local_failure(error);
    }
    let payload = serde_json::json!({
        "data_root": layout.root().to_string_lossy().into_owned(),
        "removed": true,
    });
    success(format!("{payload}\n"))
}

/// Bind the router and serve it until the listener fails.
fn serve(router: Router, host: &str, port: u16) -> Result<(), std::io::Error> {
    let runtime = tokio::runtime::Runtime::new()?;
    runtime.block_on(async move {
        let listener = tokio::net::TcpListener::bind((host, port)).await?;
        axum::serve(listener, router).await
    })
}

/// The current system-UTC instant at microsecond resolution.
fn system_now() -> DateTime {
    let micros = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_micros().min(i64::MAX as u128) as i64)
        .unwrap_or(0);
    DateTime::from_micros(micros)
}

/// Parse a `YYYY-MM-DD` session date with the clap value-parser contract.
fn parse_session_date(value: &str) -> Result<Date, String> {
    Date::parse(value).ok_or_else(|| format!("invalid session date: {value}"))
}

/// Case-insensitive environment lookup mirroring the configuration loader.
fn environment_value(environ: &BTreeMap<String, String>, key: &str) -> Option<String> {
    if let Some(value) = environ.get(key) {
        return Some(value.clone());
    }
    let wanted = key.to_ascii_uppercase();
    environ
        .iter()
        .find(|(name, _)| name.to_ascii_uppercase() == wanted)
        .map(|(_, value)| value.clone())
}

/// A successful outcome with the supplied stdout text.
fn success(stdout: String) -> CliOutcome {
    CliOutcome {
        code: 0,
        stdout,
        stderr: String::new(),
    }
}

/// A command failure printed as `lake: error: <message>`.
fn lake_failure(message: impl std::fmt::Display) -> CliOutcome {
    failure("lake", message)
}

/// A command failure printed as `lake local: error: <message>`.
fn local_failure(message: impl std::fmt::Display) -> CliOutcome {
    failure("lake local", message)
}

/// A failure outcome with the shared exit code and prefix.
fn failure(prefix: &str, message: impl std::fmt::Display) -> CliOutcome {
    CliOutcome {
        code: FAILURE_CODE,
        stdout: String::new(),
        stderr: format!("{prefix}: error: {message}\n"),
    }
}
