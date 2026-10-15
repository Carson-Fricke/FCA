//! Explicit cleanup of owned development data; the harness API hosts market data and ACI workers own ingestion.

use clap::{Parser, Subcommand};
use lake_local::LocalLayout;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
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
        Command::Local {
            command: LocalCommand::Reset { yes, data_dir },
        } => run_local_reset(yes, data_dir.as_deref(), environ),
    }
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
    /// Explicit cleanup of owned workstation data. Readers use the combined harness service.
    Local {
        #[command(subcommand)]
        command: LocalCommand,
    },
}

/// Local process and state commands.
#[derive(Debug, Subcommand)]
enum LocalCommand {
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

/// A successful outcome with the supplied stdout text.
fn success(stdout: String) -> CliOutcome {
    CliOutcome {
        code: 0,
        stdout,
        stderr: String::new(),
    }
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
