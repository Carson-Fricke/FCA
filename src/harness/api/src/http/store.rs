//! Results and job persistence ports for the harness HTTP boundary.
//!
//! The durable store is defined once in [`mcp::store`] so the axum HTTP
//! boundary and the MCP server share the same filesystem-backed
//! implementation. This module re-exports that surface under the historical
//! `api::http::store` path used across the HTTP layer.

pub use mcp::store::*;
