//! Provider-independent orchestration for bounded SQL queries.
//!
//! Ports the SQL path of `lake/query/application.py`: a SQL-mode request shares
//! the file-mode coverage classification and ensure repair, then executes one
//! bounded SELECT over only the ensured request partitions through the
//! injected [`BoundedDuckDbQuery`] engine. A missing engine is the Python
//! `query_unavailable` rejection; bounded-executor rejections propagate with
//! their own codes so the HTTP boundary can map them.

use lake_contracts::{AssetClass, DataType, Date};
use serde_json::json;

use crate::application::{
    classify_sessions, coverage_report, incomplete_error, missing_object_path,
};
use crate::bounded::{BoundedQueryError, BoundedQueryOutput, BoundedQueryResult, EnsuredQueryFile};
use crate::error::QueryApplicationError;
use crate::models::FileQueryContext;
use lake_coverage::{EnsureSpec, SessionCoverage};

/// A validated bounded SQL query over one symbol and session window.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SqlQueryRequest {
    /// Requested asset class.
    pub asset_class: AssetClass,
    /// Requested data type.
    pub data_type: DataType,
    /// Requested symbol.
    pub symbol: String,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
    /// Whether a coverage gap may be repaired by the ensure callback.
    pub ensure: bool,
    /// Bounded SQL statement.
    pub sql: String,
    /// Requested response representation.
    pub output: BoundedQueryOutput,
    /// Whether the caller asked for the authorizing force override.
    pub force: bool,
    /// Canonical bars interval token; required for bars and absent otherwise.
    pub interval: Option<String>,
}

/// The bounded query result and the representation it was rendered for.
#[derive(Clone, Debug, PartialEq)]
pub struct SqlQueryResult {
    /// The bounded result.
    pub result: BoundedQueryResult,
    /// The representation requested by the caller.
    pub output: BoundedQueryOutput,
}

/// Either an application rejection or a bounded-executor rejection.
#[derive(Clone, Debug, PartialEq)]
pub enum SqlQueryError {
    /// Coverage or ensure orchestration failure, mapped by the application
    /// error table.
    Application(QueryApplicationError),
    /// SQL validation, path, limit, or storage failure from the executor.
    Bounded(BoundedQueryError),
}

impl From<QueryApplicationError> for SqlQueryError {
    fn from(error: QueryApplicationError) -> Self {
        Self::Application(error)
    }
}

impl std::fmt::Display for SqlQueryError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Application(error) => std::fmt::Display::fmt(error, formatter),
            Self::Bounded(error) => std::fmt::Display::fmt(error, formatter),
        }
    }
}

impl std::error::Error for SqlQueryError {}

/// Ensure, classify, and execute one bounded SQL query.
pub async fn execute_sql_query(
    context: FileQueryContext<'_>,
    request: &SqlQueryRequest,
) -> Result<SqlQueryResult, SqlQueryError> {
    let spec = EnsureSpec {
        asset_class: request.asset_class,
        data_type: request.data_type,
        symbol: request.symbol.clone(),
        start_date: request.start_date,
        end_date: request.end_date,
        interval: request.interval.clone(),
    };

    let mut report = coverage_report(&context, &spec)?;
    let (mut usable, mut incomplete) = classify_sessions(
        &report,
        request.asset_class,
        request.data_type,
        request.interval.as_deref(),
        &request.symbol,
    );
    if !incomplete.is_empty() && !request.ensure {
        return Err(incomplete_error(&incomplete, false).into());
    }

    if !incomplete.is_empty() {
        let Some(callback) = context.ensure_callback.as_ref() else {
            return Err(QueryApplicationError::new(
                "ensure_unavailable",
                "coverage ensure is not configured",
                None,
            )
            .into());
        };
        callback(
            spec.asset_class,
            spec.data_type,
            spec.interval.clone(),
            spec.symbol.clone(),
            spec.start_date,
            spec.end_date,
        )
        .await
        .map_err(|error| {
            QueryApplicationError::new(
                "ensure_failed",
                "coverage ensure failed",
                Some(json!({"error": error.to_string()})),
            )
        })?;
        report = coverage_report(&context, &spec)?;
        (usable, incomplete) = classify_sessions(
            &report,
            request.asset_class,
            request.data_type,
            request.interval.as_deref(),
            &request.symbol,
        );
        if !incomplete.is_empty() {
            return Err(incomplete_error(&incomplete, true).into());
        }
    }

    let Some(bounded_query) = context.bounded_query else {
        return Err(QueryApplicationError::new(
            "query_unavailable",
            "bounded SQL query storage is not configured",
            None,
        )
        .into());
    };
    let files = ensured_files(&usable, request)?;
    let result = bounded_query.execute(
        &request.sql,
        &files,
        request.force,
        context.force_authorized,
        request.output,
    )?;
    Ok(SqlQueryResult {
        result,
        output: request.output,
    })
}

/// Map the usable entries to the engine's ensured-file contract.
fn ensured_files(
    entries: &[SessionCoverage],
    request: &SqlQueryRequest,
) -> Result<Vec<EnsuredQueryFile>, SqlQueryError> {
    let mut files = Vec::with_capacity(entries.len());
    for entry in entries {
        let Some(record) = entry.record.as_ref() else {
            return Err(missing_object_path().into());
        };
        let Some(object_path) = record.object_path.as_deref() else {
            return Err(missing_object_path().into());
        };
        files.push(
            EnsuredQueryFile::new(
                request.asset_class,
                request.data_type,
                request.symbol.clone(),
                entry.session_date(),
                object_path,
                record.byte_size,
            )
            .with_interval(request.interval.as_deref()),
        );
    }
    Ok(files)
}

/// The bounded-executor rejection for `From<BoundedQueryError>`.
impl From<BoundedQueryError> for SqlQueryError {
    fn from(error: BoundedQueryError) -> Self {
        Self::Bounded(error)
    }
}
