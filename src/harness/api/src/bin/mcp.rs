//! Standalone `mcp` process entry point.
//!
//! Mirrors the `harness.mcp.__main__` server: serve the harness MCP server
//! over stdio using the Rust implementation.

use std::process::ExitCode;

use api::mcp_cli::serve_mcp_blocking;
use api::mcp_cli::McpCli;
use clap::Parser;

fn main() -> ExitCode {
    let cli = McpCli::parse();
    serve_mcp_blocking(&cli.args)
}
