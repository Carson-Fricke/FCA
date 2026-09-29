//! Provider-independent query application for the market lake.
//!
//! This crate is the Rust port of `lake/query`: the provider-independent
//! orchestration of `application.py` and the bounded DuckDB SQL engine of
//! `service.py`. File mode validates a request, classifies its coverage,
//! repairs a gap through the injected ensure callback when the caller asked
//! for it, and returns the complete partitions with their read URLs. SQL mode
//! runs one bounded SELECT over only those ensured Parquet files through an
//! in-memory DuckDB connection, under byte limits and read-only guards. The
//! streaming read mode plans the same ensured files for the Arrow IPC route,
//! and the reference mode shapes point-in-time classification and fundamentals
//! facts for the published reference routes.

mod application;
mod bounded;
mod error;
mod models;
mod read;
mod reference;
mod sql;

pub use application::execute_file_query;
pub use bounded::{
    validate_read_only_sql, BoundedDuckDbQuery, BoundedQueryError, BoundedQueryOutput,
    BoundedQueryResult, EnsuredQueryFile, QueryLimits,
};
pub use error::QueryApplicationError;
pub use models::{
    EnsureCallback, FileQueryContext, FileQueryFile, FileQueryRequest, FileQueryResponse,
    FileQueryResults, FileUrlIssuer,
};
pub use read::{plan_from_entries, plan_read, ReadFile, ReadPlan, ReadRequest};
pub use reference::{symbol_facts, universe_symbols};
pub use sql::{execute_sql_query, SqlQueryError, SqlQueryRequest, SqlQueryResult};
