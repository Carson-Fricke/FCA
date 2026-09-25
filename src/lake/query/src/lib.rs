//! Bounded file queries over ensured lake objects.
//!
//! This crate is the Rust port of the file-query path of `lake/query`: the
//! provider-independent orchestration of `application.py` and the file mode of
//! the `POST /v1/query` route. A request is validated, its requested coverage
//! is classified, the injected ensure callback repairs a gap when the caller
//! asked for it, and the canonical complete partitions are returned with their
//! issued read URLs.
//!
//! The bounded DuckDB SQL mode is deliberately deferred: this crate executes no
//! SQL and never constructs query storage.

mod application;
mod error;
mod models;

pub use application::execute_file_query;
pub use error::QueryApplicationError;
pub use models::{
    EnsureCallback, FileQueryContext, FileQueryFile, FileQueryRequest, FileQueryResponse,
    FileQueryResults, FileUrlIssuer,
};
