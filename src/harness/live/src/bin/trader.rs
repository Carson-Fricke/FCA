//! Supervised trader process entrypoint over the Rust supervision runner.
//!
//! Loads a trader configuration document, composes the durable state store,
//! kill-switch gate, market-data boundary, and supervision journal, then runs
//! one supervised paper or shadow session to a terminal outcome. The process
//! exit code is the supervision exit code the platform observes.
//!
//! The market-data boundary is injected: this binary runs the deterministic
//! in-process paper feed supplied by the deployment composition (no network),
//! while the configured Alpaca adapter is wired by the runtime environment.

use std::path::Path;
use std::process::ExitCode;

use clap::{Parser, Subcommand};

use live::supervision::{
    read_supervision_status, run_trader_process, TraderExitCode, DEFAULT_CONFIG_PATH,
};

#[derive(Debug, Parser)]
#[command(
    name = "trader",
    about = "Run one supervised paper or shadow trading session to completion.",
    disable_help_subcommand = true
)]
struct TraderCli {
    /// Path to the trader configuration document.
    #[arg(long, value_name = "PATH", default_value = DEFAULT_CONFIG_PATH)]
    config: String,

    /// Optional subcommands.
    #[command(subcommand)]
    command: Option<TraderCommand>,
}

#[derive(Debug, Subcommand)]
enum TraderCommand {
    /// Print the latest supervision document for the configured run.
    #[command(name = "status")]
    Status,
}

fn main() -> ExitCode {
    let cli = TraderCli::parse();
    match cli.command {
        Some(TraderCommand::Status) => status(&cli.config),
        None => ExitCode::from(run_trader_process(Path::new(&cli.config)).code()),
    }
}

fn status(config_path: &str) -> ExitCode {
    match read_supervision_status(Path::new(config_path)) {
        Some(payload) => {
            println!("{payload}");
            ExitCode::SUCCESS
        }
        None => {
            eprintln!("no supervision record for {config_path}");
            ExitCode::from(TraderExitCode::Failed.code())
        }
    }
}
