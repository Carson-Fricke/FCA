//! `harness` process facade entry point.
//!
//! Mirrors the `harness` console script (`harness.cli:main`); clap handles
//! `--help` and usage errors, [`api::run`] dispatches to the selected
//! process group, and the `api`/`mcp` groups are served by the Rust process
//! implementations.

use std::process::ExitCode;

use api::{
    emit, http, mcp_cli, run, CommandResult, HarnessCli, NativeBacktestJobRunner,
    NativeStudyJobRunner,
};
use clap::Parser;

fn main() -> ExitCode {
    let cli = HarnessCli::parse();
    // Warm the embedded interpreter on the main thread: background study and
    // launch threads only acquire the GIL after this, never initialize.
    host::python::warm_interpreter();
    match run(cli, &NativeBacktestJobRunner, &NativeStudyJobRunner) {
        CommandResult::ServeApi(args) => http::cli::serve_api_blocking(&args),
        CommandResult::ServeMcp(args) => mcp_cli::serve_mcp_blocking(&args),
        CommandResult::RunTrader(args) => api::run_trader(&args),
        other => emit(other),
    }
}
