//! Authenticated file-query and bounded SQL-query routes.

use axum::body::Body;
use axum::extract::State;
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use lake_contracts::{partition_path, AssetClass, DataType, Date, DateTime};
use lake_coverage::{CoverageReport, SessionCoverage};
use lake_query::{
    execute_file_query, execute_sql_query, BoundedQueryError, BoundedQueryOutput,
    BoundedQueryResult, EnsuredQueryFile, FileQueryContext, FileQueryFile, FileQueryRequest,
    FileQueryResponse, FileQueryResults, FileUrlIssuer, SqlQueryError, SqlQueryRequest,
};
use serde::Serialize;

use crate::error::ApiError;
use crate::models::{parse_query_body, FileQueryBody, QueryBody, SqlOutput, SqlQueryBody};
use crate::selection::{contracts_or_default, option_coverage, OptionRequestLike};
use crate::state::ApiContext;
use crate::validate::StrictJson;

/// Return files or a bounded result over the requested complete files.
pub(crate) async fn query(
    State(context): State<ApiContext>,
    StrictJson(value): StrictJson,
) -> Result<Response, ApiError> {
    match parse_query_body(&value)? {
        QueryBody::File(request) => query_file(&context, &request).await,
        QueryBody::Sql(request) => query_sql(&context, &request).await,
    }
}

async fn query_file(context: &ApiContext, request: &FileQueryBody) -> Result<Response, ApiError> {
    if request.asset_class == AssetClass::Option {
        return query_option_file(context, request).await;
    }
    let as_of = context.coverage_as_of();
    let file_request = FileQueryRequest {
        asset_class: request.asset_class,
        data_type: request.data_type,
        symbol: request.symbol.clone(),
        start_date: request.start_date,
        end_date: request.end_date,
        ensure: request.ensure,
    };
    let result = execute_file_query(
        FileQueryContext {
            catalog: context.state.catalog.as_ref(),
            listings: context.state.listings.as_deref(),
            sas_issuer: context.state.sas_issuer.as_deref(),
            ensure_callback: context.state.ensure_callback.clone(),
            as_of,
            bounded_query: None,
            force_authorized: false,
        },
        &file_request,
    )
    .await
    .map_err(|error| ApiError::from_query(&error))?;
    // The Python route builds every file response through a configured issuer,
    // so a success without one is still reported as unavailable.
    if context.state.sas_issuer.is_none() {
        return Err(sas_unavailable());
    }
    let pending = result.results.pending_sessions > 0;
    let status = if pending {
        StatusCode::ACCEPTED
    } else {
        StatusCode::OK
    };
    Ok((status, Json(result)).into_response())
}

async fn query_option_file(
    context: &ApiContext,
    request: &FileQueryBody,
) -> Result<Response, ApiError> {
    let as_of = context.coverage_as_of();
    let mut report = run_option_coverage(context, request, as_of)?;
    let (mut usable, mut incomplete) = classify_option(&report, request.data_type);
    if !incomplete.is_empty() && !request.ensure {
        return Err(incomplete_error(&incomplete, false));
    }
    if !incomplete.is_empty() {
        invoke_option_ensure(context, request).await?;
        report = run_option_coverage(context, request, as_of)?;
        let (refreshed_usable, refreshed_incomplete) = classify_option(&report, request.data_type);
        usable = refreshed_usable;
        incomplete = refreshed_incomplete;
    }

    let Some(issuer) = context.state.sas_issuer.as_deref() else {
        return Err(sas_unavailable());
    };
    let files = build_option_files(&usable, issuer)?;
    let pending = !incomplete.is_empty();
    let response = file_query_response(
        files,
        report.entries.len() as u64,
        incomplete.len() as u64,
        pending,
    );
    let status = if pending {
        StatusCode::ACCEPTED
    } else {
        StatusCode::OK
    };
    Ok((status, Json(response)).into_response())
}

async fn query_sql(context: &ApiContext, request: &SqlQueryBody) -> Result<Response, ApiError> {
    if request.asset_class == AssetClass::Option {
        return query_option_sql(context, request).await;
    }
    let as_of = context.coverage_as_of();
    let sql_request = SqlQueryRequest {
        asset_class: request.asset_class,
        data_type: request.data_type,
        symbol: request.symbol.clone().unwrap_or_default(),
        start_date: request.start_date,
        end_date: request.end_date,
        ensure: request.ensure,
        sql: request.sql.clone(),
        output: bounded_output(request.output),
        force: request.force,
    };
    let result = execute_sql_query(
        FileQueryContext {
            catalog: context.state.catalog.as_ref(),
            listings: context.state.listings.as_deref(),
            sas_issuer: None,
            ensure_callback: context.state.ensure_callback.clone(),
            as_of,
            bounded_query: context.state.bounded_query.as_deref(),
            force_authorized: context.state.authorized_force_override,
        },
        &sql_request,
    )
    .await
    .map_err(map_sql_error)?;
    Ok(sql_response(&result.result, request.output))
}

async fn query_option_sql(
    context: &ApiContext,
    request: &SqlQueryBody,
) -> Result<Response, ApiError> {
    let as_of = context.coverage_as_of();
    let report = run_option_coverage(context, request, as_of)?;
    let (usable, incomplete) = classify_option(&report, request.data_type);
    if !incomplete.is_empty() && !request.ensure {
        return Err(incomplete_error(&incomplete, false));
    }
    let usable = if !incomplete.is_empty() {
        invoke_option_ensure(context, request).await?;
        let refreshed = run_option_coverage(context, request, as_of)?;
        let (usable, incomplete) = classify_option(&refreshed, request.data_type);
        if !incomplete.is_empty() {
            return Err(incomplete_error(&incomplete, true));
        }
        usable
    } else {
        usable
    };
    let Some(bounded_query) = context.state.bounded_query.as_deref() else {
        return Err(query_unavailable());
    };
    let files = option_ensured_files(&usable, request.data_type);
    let result = bounded_query.execute(
        &request.sql,
        &files,
        request.force,
        context.state.authorized_force_override,
        bounded_output(request.output),
    );
    match result {
        Ok(result) => Ok(sql_response(&result, request.output)),
        Err(error) => Err(bounded_query_error(&error)),
    }
}

/// Map the route's SQL output enum onto the bounded executor's.
fn bounded_output(output: SqlOutput) -> BoundedQueryOutput {
    match output {
        SqlOutput::Json => BoundedQueryOutput::Json,
        SqlOutput::Arrow => BoundedQueryOutput::Arrow,
    }
}

/// Map SQL orchestration failures onto the Python error envelope.
fn map_sql_error(error: SqlQueryError) -> ApiError {
    match error {
        SqlQueryError::Application(error) => ApiError::from_query(&error),
        SqlQueryError::Bounded(error) => bounded_query_error(&error),
    }
}

/// Map a bounded-executor rejection onto the Python status table.
fn bounded_query_error(error: &BoundedQueryError) -> ApiError {
    let status = match error.code.as_str() {
        "query_storage_unavailable" => StatusCode::SERVICE_UNAVAILABLE,
        "scan_limit_exceeded" | "result_limit_exceeded" => StatusCode::PAYLOAD_TOO_LARGE,
        "force_override_not_authorized" => StatusCode::FORBIDDEN,
        _ => StatusCode::UNPROCESSABLE_ENTITY,
    };
    ApiError::new(status, error.code.clone(), error.message.clone())
}

/// The strict JSON representation of a bounded SQL result.
#[derive(Serialize)]
struct SqlQueryResponse {
    /// Always `"json"`.
    format: &'static str,
    /// Column names in result order.
    columns: Vec<String>,
    /// The JSON rows.
    rows: Vec<serde_json::Value>,
    /// Serialized result size in bytes.
    result_bytes: u64,
    /// Conservative catalog/file scan estimate in bytes.
    estimated_scan_bytes: u64,
    /// Whether the authorized force override was applied.
    forced: bool,
    /// Stable response limitations.
    limitations: Vec<String>,
}

/// Render one bounded result the way the Python `_sql_response` does.
fn sql_response(result: &BoundedQueryResult, output: SqlOutput) -> Response {
    let mut limitations = vec![
        "SQL is executed only against the canonical complete request partitions.".to_string(),
        "Storage paths, mutating statements, and uncapped execution are rejected.".to_string(),
    ];
    if result.forced {
        limitations.push(
            "The authorized force override permitted a scan or result above its configured cap."
                .to_string(),
        );
    }
    match output {
        SqlOutput::Arrow => {
            let force_header = if result.forced { "true" } else { "false" };
            Response::builder()
                .status(StatusCode::OK)
                .header(
                    header::CONTENT_TYPE,
                    HeaderValue::from_static("application/vnd.apache.arrow.stream"),
                )
                .header(
                    "x-query-estimated-scan-bytes",
                    HeaderValue::from(result.estimated_scan_bytes),
                )
                .header(
                    "x-query-result-bytes",
                    HeaderValue::from(result.result_bytes),
                )
                .header(
                    "x-query-force-applied",
                    HeaderValue::from_static(force_header),
                )
                .body(Body::from(result.arrow_bytes.clone()))
                .expect("static response")
        }
        SqlOutput::Json => {
            let response = SqlQueryResponse {
                format: "json",
                columns: result.columns.clone(),
                rows: result
                    .rows
                    .iter()
                    .map(|row| serde_json::Value::Object(row.clone()))
                    .collect(),
                result_bytes: result.result_bytes,
                estimated_scan_bytes: result.estimated_scan_bytes,
                forced: result.forced,
                limitations,
            };
            (StatusCode::OK, Json(response)).into_response()
        }
    }
}

/// The selection fields shared by the file and SQL option query bodies.
trait OptionQuerySelection {
    /// Requested asset class.
    fn asset_class(&self) -> AssetClass;
    /// Requested data type.
    fn data_type(&self) -> DataType;
    /// Requested symbol.
    fn symbol(&self) -> Option<&str>;
    /// Requested underlying.
    fn underlying(&self) -> Option<&str>;
    /// Explicitly selected OSI contracts.
    fn contract_symbols(&self) -> Option<&[String]>;
    /// Alias for `contract_symbols`.
    fn contracts(&self) -> Option<&[String]>;
    /// Inclusive first session date.
    fn start_date(&self) -> Date;
    /// Inclusive last session date.
    fn end_date(&self) -> Date;
}

impl OptionQuerySelection for FileQueryBody {
    fn asset_class(&self) -> AssetClass {
        self.asset_class
    }
    fn data_type(&self) -> DataType {
        self.data_type
    }
    fn symbol(&self) -> Option<&str> {
        self.symbol.as_deref()
    }
    fn underlying(&self) -> Option<&str> {
        self.underlying.as_deref()
    }
    fn contract_symbols(&self) -> Option<&[String]> {
        self.contract_symbols.as_deref()
    }
    fn contracts(&self) -> Option<&[String]> {
        self.contracts.as_deref()
    }
    fn start_date(&self) -> Date {
        self.start_date
    }
    fn end_date(&self) -> Date {
        self.end_date
    }
}

impl OptionQuerySelection for SqlQueryBody {
    fn asset_class(&self) -> AssetClass {
        self.asset_class
    }
    fn data_type(&self) -> DataType {
        self.data_type
    }
    fn symbol(&self) -> Option<&str> {
        self.symbol.as_deref()
    }
    fn underlying(&self) -> Option<&str> {
        self.underlying.as_deref()
    }
    fn contract_symbols(&self) -> Option<&[String]> {
        self.contract_symbols.as_deref()
    }
    fn contracts(&self) -> Option<&[String]> {
        self.contracts.as_deref()
    }
    fn start_date(&self) -> Date {
        self.start_date
    }
    fn end_date(&self) -> Date {
        self.end_date
    }
}

/// Calculate option coverage for one validated query body.
fn run_option_coverage(
    context: &ApiContext,
    request: &impl OptionQuerySelection,
    as_of: DateTime,
) -> Result<CoverageReport, ApiError> {
    let contracts = contracts_or_default(context.state.contracts.as_deref());
    let like = OptionRequestLike {
        data_type: request.data_type(),
        symbol: request.symbol(),
        underlying: request.underlying(),
        contract_symbols: request.contract_symbols(),
        contracts: request.contracts(),
        start_date: request.start_date(),
        end_date: request.end_date(),
    };
    let (report, _) = option_coverage(
        context.state.catalog.as_ref(),
        &like,
        Some(contracts),
        context.state.front_sets.as_deref(),
        as_of,
    )
    .map_err(|error| ApiError::from_coverage(&error))?;
    Ok(report)
}

/// Invoke the injected coverage ensure for an incomplete option query.
async fn invoke_option_ensure(
    context: &ApiContext,
    request: &impl OptionQuerySelection,
) -> Result<(), ApiError> {
    let Some(callback) = context.state.ensure_callback.clone() else {
        return Err(ensure_unavailable());
    };
    let symbol = option_callback_symbol(request);
    callback(
        request.asset_class(),
        request.data_type(),
        symbol,
        request.start_date(),
        request.end_date(),
    )
    .await
    .map_err(|_| ensure_failed())
}

/// Flatten an option selection to the callback's single-symbol boundary.
fn option_callback_symbol(request: &impl OptionQuerySelection) -> String {
    if let Some(underlying) = request.underlying() {
        return underlying.to_string();
    }
    if let Some(symbol) = request.symbol() {
        return symbol.to_string();
    }
    request
        .contract_symbols()
        .or_else(|| request.contracts())
        .and_then(|values| values.first())
        .cloned()
        .unwrap_or_default()
}

/// Split option entries into usable complete files and incomplete sessions.
fn classify_option(
    report: &CoverageReport,
    data_type: DataType,
) -> (Vec<SessionCoverage>, Vec<SessionCoverage>) {
    let mut usable = Vec::new();
    let mut incomplete = Vec::new();
    for entry in &report.entries {
        if entry.is_complete() && option_expected_path(entry, data_type) {
            usable.push(entry.clone());
        } else {
            incomplete.push(entry.clone());
        }
    }
    (usable, incomplete)
}

/// Require complete option rows to point at their canonical OSI object.
fn option_expected_path(entry: &SessionCoverage, data_type: DataType) -> bool {
    let Some(record) = entry.record.as_ref() else {
        return false;
    };
    let Some(object_path) = record.object_path.as_deref() else {
        return false;
    };
    partition_path(
        entry.session_date(),
        AssetClass::Option,
        data_type,
        &entry.symbol,
    )
    .is_ok_and(|expected| object_path == expected)
}

/// Map complete option entries to the bounded executor's file contract,
/// skipping entries the Python `_option_ensured_files` skips.
fn option_ensured_files(entries: &[SessionCoverage], data_type: DataType) -> Vec<EnsuredQueryFile> {
    entries
        .iter()
        .filter_map(|entry| {
            let record = entry.record.as_ref()?;
            let object_path = record.object_path.as_deref()?;
            Some(EnsuredQueryFile::new(
                AssetClass::Option,
                data_type,
                entry.symbol.clone(),
                entry.session_date(),
                object_path,
                record.byte_size,
            ))
        })
        .collect()
}

/// Map complete entries to files with one issued URL each.
fn build_option_files(
    entries: &[SessionCoverage],
    issuer: &dyn FileUrlIssuer,
) -> Result<Vec<FileQueryFile>, ApiError> {
    let mut files = Vec::with_capacity(entries.len());
    for entry in entries {
        let Some(record) = entry.record.as_ref() else {
            return Err(ApiError::internal(
                "complete coverage entry has no object path",
            ));
        };
        let Some(object_path) = record.object_path.as_deref() else {
            return Err(ApiError::internal(
                "complete coverage entry has no object path",
            ));
        };
        let url = issuer.issue(object_path).map_err(|error| {
            ApiError::sas_unavailable(format!("file query SAS issuance failed: {error}"))
        })?;
        files.push(FileQueryFile {
            asset_class: entry.asset_class,
            data_type: entry.data_type,
            symbol: entry.symbol.clone(),
            session_date: entry.session_date(),
            object_path: object_path.to_string(),
            url: url.clone(),
            sas_url: Some(url),
            row_count: record.row_count,
            byte_size: record.byte_size,
            checksum: record.checksum.clone(),
        });
    }
    Ok(files)
}

/// Build the file-query response shape the Python route renders.
fn file_query_response(
    files: Vec<FileQueryFile>,
    requested_sessions: u64,
    pending_sessions: u64,
    ensure_pending: bool,
) -> FileQueryResponse {
    let mut limitations = vec![
        "Only complete catalog partitions are returned.".to_string(),
        "URLs are read-only, object-scoped, and expire according to SAS policy.".to_string(),
    ];
    if ensure_pending {
        limitations.push("Coverage ensure was requested but is not complete yet.".to_string());
    }
    FileQueryResponse {
        results: FileQueryResults {
            requested_sessions,
            returned_files: files.len() as u64,
            complete_sessions: files.len() as u64,
            pending_sessions,
            state: if ensure_pending {
                "ensure_pending".to_string()
            } else {
                "complete".to_string()
            },
        },
        files,
        limitations,
    }
}

/// The Python `coverage_incomplete` rejection; details do not cross the boundary.
fn incomplete_error(_entries: &[SessionCoverage], ensure_requested: bool) -> ApiError {
    let message = if ensure_requested {
        "coverage remains incomplete after ensure"
    } else {
        "requested coverage is incomplete; set ensure=true to request coverage"
    };
    ApiError::new(StatusCode::CONFLICT, "coverage_incomplete", message)
}

/// The Python `ensure_unavailable` rejection.
fn ensure_unavailable() -> ApiError {
    ApiError::new(
        StatusCode::SERVICE_UNAVAILABLE,
        "ensure_unavailable",
        "coverage ensure is not configured",
    )
}

/// The Python `ensure_failed` rejection.
fn ensure_failed() -> ApiError {
    ApiError::new(
        StatusCode::BAD_GATEWAY,
        "ensure_failed",
        "coverage ensure failed",
    )
}

/// The Python `sas_unavailable` rejection for the file query.
fn sas_unavailable() -> ApiError {
    ApiError::sas_unavailable("file query SAS issuance is not configured")
}

/// The Python `query_unavailable` rejection when no engine is configured.
fn query_unavailable() -> ApiError {
    ApiError::new(
        StatusCode::SERVICE_UNAVAILABLE,
        "query_unavailable",
        "bounded SQL query storage is not configured",
    )
}
