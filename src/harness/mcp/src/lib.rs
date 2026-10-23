//! JSON-RPC 2.0 / MCP server for the quant-harness Rust port.
//!
//! This crate ports `harness.mcp` onto Rust: the capability-filtered tool
//! catalog, the pure JSON-RPC dispatch, the bearer credential resolution, and
//! the newline-delimited stdio transport. Every external boundary (process
//! runner, remote HTTP transport, configuration loader) is
//! injected, so the server is exercised without spawning processes or
//! touching the network.

pub mod auth;
pub mod server;
pub mod tools;
pub mod transport;

pub use server::{
    create_server, create_server_named, error_response, McpServer, PROTOCOL_VERSION, SERVER_NAME,
    SERVER_VERSION,
};
pub use tools::catalog::{build_catalog, ToolCatalog};
pub use tools::ports::{HarnessToolsContext, ToolCapabilities};
pub use transport::{decode_message, encode_message, serve_stdio, InProcessTransport};
