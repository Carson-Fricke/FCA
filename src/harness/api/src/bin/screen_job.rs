//! Standalone `screen-job` process entry point.

use std::process::ExitCode;

use api::{emit, run_screen_job, NativeScreenJobRunner, ScreenJobCli};
use clap::Parser;

fn main() -> ExitCode {
    let cli = ScreenJobCli::parse();
    emit(run_screen_job(cli, &NativeScreenJobRunner))
}
