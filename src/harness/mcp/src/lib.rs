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
pub mod store;
pub mod tools;
pub mod transport;

pub use server::{
    create_server, create_server_named, error_response, McpServer, PROTOCOL_VERSION, SERVER_NAME,
    SERVER_VERSION,
};
pub use store::{
    artifact_digest, canonical_json, run_digest, run_prefix, FileSystemJobRepository,
    FileSystemRunStore, InMemoryJobRepository, InMemoryRunStore, JobRecord, JobRepository,
    JobState, RunCatalogRecord, RunStatus, RunStore, SasIssuer, StoreError, StoredRun,
};
pub use tools::{build_catalog, HarnessTools, HarnessToolsContext, ToolCapabilities, ToolCatalog};
pub use transport::{decode_message, encode_message, serve_stdio, InProcessTransport};
