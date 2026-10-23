//! Standalone `backtest-job` process entry point.
//!
//! Mirrors the `backtest-job` console script
//! (`harness.cli.backtest_job:main`).

use std::process::ExitCode;

use api::emit;
use api::run_backtest_job;
use api::BacktestJobCli;
use api::NativeBacktestJobRunner;
use clap::Parser;

fn main() -> ExitCode {
    let cli = BacktestJobCli::parse();
    emit(run_backtest_job(cli, &NativeBacktestJobRunner))
}
