//! Run one deterministic backtest job against a configuration document.
//!
//! Mirrors the `backtest-job --config <path> [--output-dir <dir>]` process
//! boundary of the Python harness on the staged Rust engine.

use std::path::PathBuf;
use std::process::ExitCode;

use backtest::backtest::job::run_backtest_job;

fn main() -> ExitCode {
    let mut config: Option<PathBuf> = None;
    let mut output: Option<PathBuf> = None;
    let mut args = std::env::args().skip(1);
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--config" => config = args.next().map(PathBuf::from),
            "--output-dir" => output = args.next().map(PathBuf::from),
            other => {
                eprintln!("unknown argument: {other}");
                return ExitCode::from(2);
            }
        }
    }
    let Some(config) = config else {
        eprintln!("A backtest configuration is required: backtest-job --config <path>");
        return ExitCode::from(2);
    };
    match run_backtest_job(&config, output.as_deref()) {
        Ok(summary) => {
            for line in summary.stdout_lines() {
                println!("{line}");
            }
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("Backtest failed: {error}");
            ExitCode::from(1)
        }
    }
}
