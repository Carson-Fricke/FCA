//! Azure composition for the harness-hosted market-data HTTP capability. Provider execution belongs to lake-ingest-worker.

pub mod access;
pub mod api;
mod local;
pub mod runtime;

pub use runtime::{
    hosted_overrides, HostedEnsureError, HostedOverrides, HostedRuntime, DEFAULT_COVERAGE_TABLE,
    DEFAULT_STORAGE_CONTAINER,
};
