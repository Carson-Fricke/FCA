//! Standalone `api` process entry point.
//!
//! Mirrors the `api` console script (`harness.cli.api:main`): serve the
//! quant-harness HTTP boundary with the axum stack.

use std::process::ExitCode;

use api::http::cli::{serve_api_blocking, ApiCli};
use clap::Parser;

fn main() -> ExitCode {
    let cli = ApiCli::parse();
    serve_api_blocking(&cli.args)
}
