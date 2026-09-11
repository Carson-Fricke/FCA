//! `harness` process facade entry point.
//!
//! Mirrors the `harness` console script (`harness.cli:main`); clap handles
//! `--help` and usage errors, [`harness_api::run`] dispatches to the selected
//! process group, and the `api`/`mcp` groups are served by the Rust process
//! implementations.

use std::process::ExitCode;

use clap::Parser;
use harness_api::{emit, http, mcp_cli, run, CommandResult, HarnessCli, NativeBacktestJobRunner};

fn main() -> ExitCode {
    let cli = HarnessCli::parse();
    match run(cli, &NativeBacktestJobRunner) {
        CommandResult::ServeApi(args) => http::cli::serve_api_blocking(&args),
        CommandResult::ServeMcp(args) => mcp_cli::serve_mcp_blocking(&args),
        other => emit(other),
    }
}
