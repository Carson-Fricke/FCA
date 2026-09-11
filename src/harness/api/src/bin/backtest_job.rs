//! Standalone `backtest-job` process entry point.
//!
//! Mirrors the `backtest-job` console script
//! (`harness.cli.backtest_job:main`).

use std::process::ExitCode;

use api::{emit, run_backtest_job, BacktestJobCli, NativeBacktestJobRunner};
use clap::Parser;

fn main() -> ExitCode {
    let cli = BacktestJobCli::parse();
    emit(run_backtest_job(cli, &NativeBacktestJobRunner))
}
