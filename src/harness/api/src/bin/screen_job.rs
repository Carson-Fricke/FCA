//! Standalone `screen-job` process entry point.

use std::process::ExitCode;

use api::emit;
use api::run_screen_job;
use api::NativeScreenJobRunner;
use api::ScreenJobCli;
use clap::Parser;

fn main() -> ExitCode {
    let cli = ScreenJobCli::parse();
    emit(run_screen_job(cli, &NativeScreenJobRunner))
}
