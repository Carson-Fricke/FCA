//! `harness` process facade entry point.
//!
//! Mirrors the `harness` console script (`harness.cli:main`); clap handles
//! `--help` and usage errors, and [`harness_cli::emit`] maps the command
//! result to the Typer exit-code contract.

use std::process::ExitCode;

use clap::Parser;
use harness_cli::{emit, run, HarnessCli, UnportedBacktestJobRunner};

fn main() -> ExitCode {
    let cli = HarnessCli::parse();
    emit(run(cli, &UnportedBacktestJobRunner))
}
