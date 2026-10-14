//! Strict request and response models for the market-lake HTTP boundary.
//!
//! Field names, ordering, and nullability mirror `lake.http.models` and
//! `lake.http.routes`, so existing clients decode responses unchanged.

use lake_contracts::{AssetClass, DataType, Date, DateTime};
use lake_coverage::{CoverageReport, SessionCoverage};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

use crate::error::ApiError;
use crate::validate::{
    optional_bool, optional_string, optional_string_list, parse_asset_class, parse_data_type,
    parse_date, parse_interval, reject_unknown, required_string, validate_date_range,
    validate_optional_symbol, validate_optional_symbol_list, validate_symbol_field,
};

/// Serialize a [`Date`] as `YYYY-MM-DD`.
pub(crate) mod date_ser {
    use lake_contracts::Date;
    use serde::Serializer;

    /// Render the date with Python's `date.isoformat()` shape.
    pub fn serialize<S>(value: &Date, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&value.isoformat())
    }
}

/// Serialize a [`DateTime`] as `...Z`, seconds precision when whole.
pub(crate) mod datetime_ser {
    use lake_contracts::DateTime;
    use serde::Serializer;

    /// Render the instant the way Pydantic renders a UTC datetime.
    pub fn serialize<S>(value: &DateTime, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&value.isoformat_seconds_z())
    }
}

/// Probe-safe liveness response.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct HealthResponse {
    /// Always `"ok"`.
    pub status: &'static str,
}

/// Credential-check response.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AuthCheckResponse {
    /// Always `"ok"`.
    pub status: &'static str,
}

/// Calculated status and catalog metadata for one expected session.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CoverageEntryResponse {
    /// Session the entry describes.
    #[serde(serialize_with = "date_ser::serialize")]
    pub session_date: Date,
    /// Inclusive UTC session open.
    #[serde(serialize_with = "datetime_ser::serialize")]
    pub open_time: DateTime,
    /// Exclusive UTC session close.
    #[serde(serialize_with = "datetime_ser::serialize")]
    pub close_time: DateTime,
    /// Whether the session is currently trading.
    pub is_partial: bool,
    /// Coverage classification.
    pub status: String,
    /// Canonical object path of the written partition, if any.
    pub object_path: Option<String>,
    /// Rows written for this symbol-day.
    pub row_count: Option<u64>,
    /// Bytes written for this symbol-day.
    pub byte_size: Option<u64>,
    /// Content digest of the written object, if any.
    pub checksum: Option<String>,
    /// Failure detail when the status is `error`.
    pub error_message: Option<String>,
}

impl CoverageEntryResponse {
    /// Render one classified session with its catalog metadata.
    #[must_use]
    pub fn from_session(entry: &SessionCoverage) -> Self {
        let record = entry.record.as_ref();
        Self {
            session_date: entry.session_date(),
            open_time: entry.session.open_time,
            close_time: entry.session.close_time,
            is_partial: entry.session.is_partial,
            status: entry.state.value().to_string(),
            object_path: record.and_then(|record| record.object_path.clone()),
            row_count: record.map(|record| record.row_count),
            byte_size: record.map(|record| record.byte_size),
            checksum: record.and_then(|record| record.checksum.clone()),
            error_message: record.and_then(|record| record.error_message.clone()),
        }
    }
}

/// Calendar-aware coverage for one symbol, data type, and date range.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CatalogCoverageResponse {
    /// Asset class of the report.
    pub asset_class: String,
    /// Data type of the report.
    pub data_type: String,
    /// Canonical uppercase symbol.
    pub symbol: String,
    /// Inclusive first session date.
    #[serde(serialize_with = "date_ser::serialize")]
    pub start_date: Date,
    /// Inclusive last session date.
    #[serde(serialize_with = "date_ser::serialize")]
    pub end_date: Date,
    /// Classifications in session order.
    pub entries: Vec<CoverageEntryResponse>,
}

impl CatalogCoverageResponse {
    /// Build the equity/crypto coverage response from a report.
    #[must_use]
    pub fn from_report(report: &CoverageReport, data_type: DataType, symbol: &str) -> Self {
        Self {
            asset_class: report.asset_class.value().to_string(),
            data_type: data_type.value().to_string(),
            symbol: symbol.trim().to_uppercase(),
            start_date: report.start_date,
            end_date: report.end_date,
            entries: report
                .entries
                .iter()
                .map(CoverageEntryResponse::from_session)
                .collect(),
        }
    }
}

/// Coverage metadata for one selected OSI contract and session.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct OptionCoverageEntryResponse {
    /// Session the entry describes.
    #[serde(serialize_with = "date_ser::serialize")]
    pub session_date: Date,
    /// Inclusive UTC session open.
    #[serde(serialize_with = "datetime_ser::serialize")]
    pub open_time: DateTime,
    /// Exclusive UTC session close.
    #[serde(serialize_with = "datetime_ser::serialize")]
    pub close_time: DateTime,
    /// Whether the session is currently trading.
    pub is_partial: bool,
    /// Coverage classification.
    pub status: String,
    /// Canonical object path of the written partition, if any.
    pub object_path: Option<String>,
    /// Rows written for this symbol-day.
    pub row_count: Option<u64>,
    /// Bytes written for this symbol-day.
    pub byte_size: Option<u64>,
    /// Content digest of the written object, if any.
    pub checksum: Option<String>,
    /// Failure detail when the status is `error`.
    pub error_message: Option<String>,
    /// Canonical OSI contract symbol.
    pub contract_symbol: String,
}

impl OptionCoverageEntryResponse {
    /// Render one selected contract session.
    #[must_use]
    pub fn from_session(entry: &SessionCoverage) -> Self {
        let record = entry.record.as_ref();
        Self {
            session_date: entry.session_date(),
            open_time: entry.session.open_time,
            close_time: entry.session.close_time,
            is_partial: entry.session.is_partial,
            status: entry.state.value().to_string(),
            object_path: record.and_then(|record| record.object_path.clone()),
            row_count: record.map(|record| record.row_count),
            byte_size: record.map(|record| record.byte_size),
            checksum: record.and_then(|record| record.checksum.clone()),
            error_message: record.and_then(|record| record.error_message.clone()),
            contract_symbol: entry.symbol.clone(),
        }
    }
}

/// Calendar-aware coverage for selected option contracts.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct OptionCatalogCoverageResponse {
    /// Always `"option"`.
    pub asset_class: String,
    /// Data type of the report.
    pub data_type: String,
    /// Canonical uppercase underlying, when resolved.
    pub underlying: Option<String>,
    /// Selected OSI contracts in deterministic order.
    pub contracts: Vec<String>,
    /// Inclusive first session date.
    #[serde(serialize_with = "date_ser::serialize")]
    pub start_date: Date,
    /// Inclusive last session date.
    #[serde(serialize_with = "date_ser::serialize")]
    pub end_date: Date,
    /// Classifications in report order.
    pub entries: Vec<OptionCoverageEntryResponse>,
}

/// Either coverage shape returned by `/v1/catalog/coverage`.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(untagged)]
pub enum CoverageResponseBody {
    /// Equity or crypto coverage.
    Standard(CatalogCoverageResponse),
    /// Selected option coverage.
    Option(OptionCatalogCoverageResponse),
}

/// An ensure request that continues as an asynchronous job.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EnsureAcceptedResponse {
    /// Stable job identifier.
    pub job_id: String,
    /// Current job state.
    pub state: String,
}

/// The persisted state of one ensure job.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct JobStatusResponse {
    /// Stable job identifier.
    pub job_id: String,
    /// Current job state.
    pub state: String,
    /// UTC creation instant.
    #[serde(serialize_with = "datetime_ser::serialize")]
    pub created_at: DateTime,
    /// UTC instant of the last state change.
    #[serde(serialize_with = "datetime_ser::serialize")]
    pub updated_at: DateTime,
    /// Failure detail when the job failed.
    pub error_message: Option<String>,
    /// Incremental progress while the job runs; absent before the first
    /// session finishes.
    pub progress: Option<lake_catalog::JobProgress>,
}

/// Catalog metadata and a read-only URL for one known lake object.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RawObjectResponse {
    /// Asset class of the object.
    pub asset_class: String,
    /// Data type of the object.
    pub data_type: String,
    /// Canonical uppercase symbol.
    pub symbol: String,
    /// Session the object covers.
    #[serde(serialize_with = "date_ser::serialize")]
    pub session_date: Date,
    /// Persisted coverage status.
    pub status: String,
    /// Canonical object path.
    pub object_path: String,
    /// Issued read-only URL.
    pub url: String,
    /// The same issued read-only URL.
    pub sas_url: String,
    /// Rows written for this symbol-day.
    pub row_count: u64,
    /// Bytes written for this symbol-day.
    pub byte_size: u64,
    /// Content digest of the written object, if any.
    pub checksum: Option<String>,
}

/// The raw query parameters of `GET /v1/read`.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ReadQueryRaw {
    /// Requested asset class.
    pub asset_class: String,
    /// Requested data type.
    pub data_type: String,
    /// Requested symbol.
    #[serde(default)]
    pub symbol: Option<String>,
    /// Requested bars interval token.
    #[serde(default)]
    pub interval: Option<String>,
    /// Inclusive first session date.
    pub start_date: String,
    /// Inclusive last session date.
    pub end_date: String,
}

/// A validated streaming-read request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ReadQuery {
    /// Requested asset class.
    pub asset_class: AssetClass,
    /// Requested data type.
    pub data_type: DataType,
    /// Requested symbol.
    pub symbol: String,
    /// Validated bars interval token.
    pub interval: Option<String>,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
}

impl ReadQuery {
    /// Validate the strict query parameters for the read route.
    pub(crate) fn parse(raw: ReadQueryRaw) -> Result<Self, ApiError> {
        let asset_class = parse_asset_class(&raw.asset_class)?;
        if asset_class == AssetClass::Reference {
            return Err(ApiError::validation_error(
                "asset_class",
                "Input should be 'equity', 'crypto' or 'option'",
            ));
        }
        let data_type = parse_data_type(&raw.data_type)?;
        if !matches!(
            data_type,
            DataType::Trade | DataType::Quote | DataType::Bars
        ) {
            return Err(ApiError::validation_error(
                "data_type",
                "Input should be 'trade', 'quote' or 'bars'",
            ));
        }
        let interval = parse_interval(data_type, raw.interval)?;
        let Some(symbol) = validate_optional_symbol(raw.symbol, "symbol")? else {
            return Err(ApiError::validation_error(
                "symbol",
                "Field required: symbol",
            ));
        };
        let start_date = parse_date(&raw.start_date, "start_date")?;
        let end_date = parse_date(&raw.end_date, "end_date")?;
        validate_date_range(start_date, end_date)?;
        Ok(Self {
            asset_class,
            data_type,
            symbol,
            interval,
            start_date,
            end_date,
        })
    }
}

/// The strict query parameters of `GET /v1/reference/universe`.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct UniverseQueryRaw {
    /// The point-in-time as-of date.
    pub as_of: String,
}

/// The strict query parameters of `GET /v1/reference/facts`.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct FactsQueryRaw {
    /// The point-in-time as-of date.
    pub as_of: String,
    /// Comma-separated requested symbols.
    pub symbols: String,
}

/// Parse the comma-separated `symbols` query value.
pub(crate) fn parse_symbols_query(value: &str) -> Result<Vec<String>, ApiError> {
    let symbols = value
        .split(',')
        .map(str::trim)
        .filter(|symbol| !symbol.is_empty())
        .map(|symbol| validate_symbol_field(symbol, "symbols"))
        .collect::<Result<Vec<_>, _>>()?;
    if symbols.is_empty() || symbols.len() > 100 {
        return Err(ApiError::validation_error(
            "symbols",
            "List should have at least 1 item and at most 100 items",
        ));
    }
    Ok(symbols)
}

/// The point-in-time equity universe known at one date.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct UniverseResponse {
    /// The requested as-of date.
    #[serde(serialize_with = "date_ser::serialize")]
    pub as_of: Date,
    /// Canonical uppercase symbols in ascending order.
    pub symbols: Vec<String>,
}

/// Point-in-time classification and fundamentals facts by symbol.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct FactsResponse {
    /// The requested as-of date.
    #[serde(serialize_with = "date_ser::serialize")]
    pub as_of: Date,
    /// One fact sheet per requested canonical symbol.
    pub facts: BTreeMap<String, BTreeMap<String, String>>,
}

/// The raw query parameters of `GET /v1/catalog/coverage`.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct CoverageQueryRaw {
    /// Requested asset class.
    pub asset_class: String,
    /// Requested data type.
    pub data_type: String,
    /// Inclusive first session date.
    pub start_date: String,
    /// Inclusive last session date.
    pub end_date: String,
    /// Requested symbol.
    #[serde(default)]
    pub symbol: Option<String>,
    /// Requested option underlying.
    #[serde(default)]
    pub underlying: Option<String>,
    /// Explicitly selected OSI contracts.
    #[serde(default)]
    pub contract_symbols: Option<Vec<String>>,
    /// Requested bars interval token.
    #[serde(default)]
    pub interval: Option<String>,
}

/// The strict query parameters of `GET /v1/raw`.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RawObjectQueryRaw {
    /// Requested canonical object path.
    pub object_path: String,
}

/// A validated equity or crypto coverage request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct CoverageQuery {
    /// Requested asset class.
    pub asset_class: AssetClass,
    /// Requested data type.
    pub data_type: DataType,
    /// Requested symbol.
    pub symbol: Option<String>,
    /// Requested option underlying.
    pub underlying: Option<String>,
    /// Explicitly selected OSI contracts.
    pub contract_symbols: Option<Vec<String>>,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
    /// Validated bars interval token.
    pub interval: Option<String>,
}

impl CoverageQuery {
    /// Validate the strict query parameters, mirroring the Pydantic model.
    pub(crate) fn parse(raw: CoverageQueryRaw) -> Result<Self, ApiError> {
        let asset_class = parse_asset_class(&raw.asset_class)?;
        let data_type = parse_data_type(&raw.data_type)?;
        let interval = parse_interval(data_type, raw.interval)?;
        let start_date = parse_date(&raw.start_date, "start_date")?;
        let end_date = parse_date(&raw.end_date, "end_date")?;
        validate_date_range(start_date, end_date)?;
        let symbol = validate_optional_symbol(raw.symbol, "symbol")?;
        let underlying = validate_optional_symbol(raw.underlying, "underlying")?;
        let contract_symbols =
            validate_optional_symbol_list(raw.contract_symbols, "contract_symbols")?;

        if asset_class != AssetClass::Option && symbol.is_none() {
            return Err(ApiError::validation_error(
                "symbol",
                "Value error, symbol is required for equity and crypto coverage",
            ));
        }
        if (contract_symbols.is_some()) && asset_class != AssetClass::Option {
            return Err(ApiError::validation_error(
                "contract_symbols",
                "Value error, contract_symbols are supported for options only",
            ));
        }
        if underlying.is_some() && asset_class != AssetClass::Option {
            return Err(ApiError::validation_error(
                "underlying",
                "Value error, underlying is supported for options only",
            ));
        }
        if asset_class == AssetClass::Option
            && symbol.is_none()
            && underlying.is_none()
            && contract_symbols.is_none()
        {
            return Err(ApiError::validation_error(
                "underlying",
                "Value error, an option underlying or selected contract is required",
            ));
        }

        Ok(Self {
            asset_class,
            data_type,
            symbol,
            underlying,
            contract_symbols,
            start_date,
            end_date,
            interval,
        })
    }
}

/// A validated equity or crypto ensure body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct EquityEnsureRequest {
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
    /// Validated bars interval token.
    pub interval: Option<String>,
}

/// A validated option ensure body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct OptionEnsureRequest {
    /// Requested data type.
    pub data_type: DataType,
    pub interval: Option<String>,
    /// Legacy single-symbol selector.
    pub symbol: Option<String>,
    /// Requested underlying.
    pub underlying: Option<String>,
    /// Explicitly selected OSI contracts.
    pub contract_symbols: Option<Vec<String>>,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
}

/// Either strict ensure body accepted by `POST /v1/catalog/ensure`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum EnsureBody {
    /// Equity or crypto ensure.
    Equity(EquityEnsureRequest),
    /// Option selection ensure.
    Option(OptionEnsureRequest),
}

/// Parse the union ensure body, selecting the variant by asset class.
pub(crate) fn parse_ensure_body(value: &serde_json::Value) -> Result<EnsureBody, ApiError> {
    let object = crate::validate::require_object(value)?;
    if object
        .get("asset_class")
        .and_then(serde_json::Value::as_str)
        == Some("option")
    {
        parse_option_ensure(object).map(EnsureBody::Option)
    } else {
        parse_equity_ensure(object).map(EnsureBody::Equity)
    }
}

fn parse_equity_ensure(
    object: &serde_json::Map<String, serde_json::Value>,
) -> Result<EquityEnsureRequest, ApiError> {
    reject_unknown(
        object,
        &[
            "asset_class",
            "data_type",
            "symbol",
            "start_date",
            "end_date",
            "interval",
        ],
    )?;
    let asset_class = parse_asset_class(&required_string(object, "asset_class")?)?;
    let data_type = parse_data_type(&required_string(object, "data_type")?)?;
    let interval = parse_interval(data_type, optional_string(object, "interval")?)?;
    let symbol = validate_symbol_field(&required_string(object, "symbol")?, "symbol")?;
    let start_date = parse_date(&required_string(object, "start_date")?, "start_date")?;
    let end_date = parse_date(&required_string(object, "end_date")?, "end_date")?;
    validate_date_range(start_date, end_date)?;
    Ok(EquityEnsureRequest {
        asset_class,
        data_type,
        symbol,
        start_date,
        end_date,
        interval,
    })
}

fn parse_option_ensure(
    object: &serde_json::Map<String, serde_json::Value>,
) -> Result<OptionEnsureRequest, ApiError> {
    reject_unknown(
        object,
        &[
            "asset_class",
            "data_type",
            "symbol",
            "underlying",
            "contract_symbols",
            "start_date",
            "end_date",
            "interval",
        ],
    )?;
    let data_type = parse_data_type(&required_string(object, "data_type")?)?;
    let interval = parse_interval(data_type, optional_string(object, "interval")?)?;
    let symbol = validate_optional_symbol(optional_string(object, "symbol")?, "symbol")?;
    let underlying =
        validate_optional_symbol(optional_string(object, "underlying")?, "underlying")?;
    let contract_symbols = validate_optional_symbol_list(
        optional_string_list(object, "contract_symbols")?,
        "contract_symbols",
    )?;
    let start_date = parse_date(&required_string(object, "start_date")?, "start_date")?;
    let end_date = parse_date(&required_string(object, "end_date")?, "end_date")?;
    validate_date_range(start_date, end_date)?;

    if symbol.is_none() && underlying.is_none() && contract_symbols.is_none() {
        return Err(ApiError::validation_error(
            "underlying",
            "Value error, an option underlying or selected contract is required",
        ));
    }
    Ok(OptionEnsureRequest {
        data_type,
        interval,
        symbol,
        underlying,
        contract_symbols,
        start_date,
        end_date,
    })
}

/// A validated file-mode query body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct FileQueryBody {
    /// Requested asset class.
    pub asset_class: AssetClass,
    /// Requested data type.
    pub data_type: DataType,
    /// Requested symbol.
    pub symbol: Option<String>,
    /// Requested option underlying.
    pub underlying: Option<String>,
    /// Explicitly selected OSI contracts.
    pub contract_symbols: Option<Vec<String>>,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
    /// Whether a coverage gap may be repaired by the ensure callback.
    pub ensure: bool,
    /// Validated bars interval token.
    pub interval: Option<String>,
}

/// The SQL response representation requested by the client.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum SqlOutput {
    /// JSON rows.
    Json,
    /// Arrow IPC stream.
    Arrow,
}

/// A validated SQL-mode query body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SqlQueryBody {
    /// Requested asset class.
    pub asset_class: AssetClass,
    /// Requested data type.
    pub data_type: DataType,
    /// Requested symbol.
    pub symbol: Option<String>,
    /// Requested option underlying.
    pub underlying: Option<String>,
    /// Explicitly selected OSI contracts.
    pub contract_symbols: Option<Vec<String>>,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
    /// Whether a coverage gap may be repaired by the ensure callback.
    pub ensure: bool,
    /// Bounded SQL statement.
    pub sql: String,
    /// Requested response representation.
    pub output: SqlOutput,
    /// Whether the caller asked for the authorizing force override.
    pub force: bool,
    /// Validated bars interval token.
    pub interval: Option<String>,
}

/// Either discriminated query body accepted by `POST /v1/query`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum QueryBody {
    /// File mode.
    File(FileQueryBody),
    /// SQL mode.
    Sql(SqlQueryBody),
}

/// Parse the mode-discriminated query body.
pub(crate) fn parse_query_body(value: &serde_json::Value) -> Result<QueryBody, ApiError> {
    let object = crate::validate::require_object(value)?;
    match object.get("mode").and_then(serde_json::Value::as_str) {
        Some("file") => parse_file_query(object).map(QueryBody::File),
        Some("sql") => parse_sql_query(object).map(QueryBody::Sql),
        Some(other) => Err(ApiError::validation_error(
            "mode",
            format!("Input should be 'file' or 'sql': {other}"),
        )),
        None => Err(ApiError::validation_error("mode", "Field required: mode")),
    }
}

fn parse_file_query(
    object: &serde_json::Map<String, serde_json::Value>,
) -> Result<FileQueryBody, ApiError> {
    reject_unknown(
        object,
        &[
            "mode",
            "asset_class",
            "data_type",
            "symbol",
            "underlying",
            "contract_symbols",
            "start_date",
            "end_date",
            "ensure",
            "interval",
        ],
    )?;
    let body = parse_query_selection(object)?;
    let ensure = optional_bool(object, "ensure", false)?;
    Ok(FileQueryBody {
        asset_class: body.asset_class,
        data_type: body.data_type,
        symbol: body.symbol,
        underlying: body.underlying,
        contract_symbols: body.contract_symbols,
        start_date: body.start_date,
        end_date: body.end_date,
        ensure,
        interval: body.interval,
    })
}

fn parse_sql_query(
    object: &serde_json::Map<String, serde_json::Value>,
) -> Result<SqlQueryBody, ApiError> {
    reject_unknown(
        object,
        &[
            "mode",
            "asset_class",
            "data_type",
            "symbol",
            "underlying",
            "contract_symbols",
            "start_date",
            "end_date",
            "ensure",
            "sql",
            "output",
            "force",
            "interval",
        ],
    )?;
    let body = parse_query_selection(object)?;
    let ensure = optional_bool(object, "ensure", false)?;
    let sql = required_string(object, "sql")?;
    let length = sql.chars().count();
    if length == 0 || length > 100_000 {
        return Err(ApiError::validation_error(
            "sql",
            "String should have at least 1 character and at most 100000 characters",
        ));
    }
    let output = match optional_string(object, "output")? {
        None => SqlOutput::Json,
        Some(value) if value == "json" => SqlOutput::Json,
        Some(value) if value == "arrow" => SqlOutput::Arrow,
        Some(_) => {
            return Err(ApiError::validation_error(
                "output",
                "Input should be 'json' or 'arrow'",
            ))
        }
    };
    let force = optional_bool(object, "force", false)?;
    Ok(SqlQueryBody {
        asset_class: body.asset_class,
        data_type: body.data_type,
        symbol: body.symbol,
        underlying: body.underlying,
        contract_symbols: body.contract_symbols,
        start_date: body.start_date,
        end_date: body.end_date,
        ensure,
        sql,
        output,
        force,
        interval: body.interval,
    })
}

/// Fields shared by the file and SQL query variants.
struct QuerySelection {
    asset_class: AssetClass,
    data_type: DataType,
    symbol: Option<String>,
    underlying: Option<String>,
    contract_symbols: Option<Vec<String>>,
    start_date: Date,
    end_date: Date,
    interval: Option<String>,
}

fn parse_query_selection(
    object: &serde_json::Map<String, serde_json::Value>,
) -> Result<QuerySelection, ApiError> {
    let asset_class = parse_asset_class(&required_string(object, "asset_class")?)?;
    let data_type = parse_data_type(&required_string(object, "data_type")?)?;
    let interval = parse_interval(data_type, optional_string(object, "interval")?)?;
    let symbol = validate_optional_symbol(optional_string(object, "symbol")?, "symbol")?;
    let underlying =
        validate_optional_symbol(optional_string(object, "underlying")?, "underlying")?;
    let contract_symbols = validate_optional_symbol_list(
        optional_string_list(object, "contract_symbols")?,
        "contract_symbols",
    )?;
    let start_date = parse_date(&required_string(object, "start_date")?, "start_date")?;
    let end_date = parse_date(&required_string(object, "end_date")?, "end_date")?;
    validate_date_range(start_date, end_date)?;

    if asset_class != AssetClass::Option {
        if symbol.is_none() {
            return Err(ApiError::validation_error(
                "symbol",
                "Value error, symbol is required for equity and crypto queries",
            ));
        }
        if underlying.is_some() || contract_symbols.is_some() {
            return Err(ApiError::validation_error(
                "underlying",
                "Value error, option selection fields are supported for options only",
            ));
        }
    } else {
        if symbol.is_none() && underlying.is_none() && contract_symbols.is_none() {
            return Err(ApiError::validation_error(
                "underlying",
                "Value error, an option underlying or selected contract is required",
            ));
        }
    }

    Ok(QuerySelection {
        asset_class,
        data_type,
        symbol,
        underlying,
        contract_symbols,
        start_date,
        end_date,
        interval,
    })
}
