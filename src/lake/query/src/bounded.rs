//! Bounded, provider-independent DuckDB queries over ensured Parquet files.
//!
//! Ports `lake/query/service.py`: one in-memory DuckDB connection sees only the
//! canonical local Parquet files the catalog marked complete, exposed as a
//! single relation named `data`. The statement is validated lexically before
//! and after binding (`EXPLAIN` plan scan), byte limits gate both the
//! conservative catalog scan estimate and the serialized result, and `force`
//! requires server-side authorization.

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use duckdb::arrow::array::{Array, AsArray, LargeStringArray, RecordBatch, StringArray};
use duckdb::arrow::datatypes::{
    DataType as ArrowType, Date32Type, Date64Type, Decimal128Type, DurationMicrosecondType,
    DurationMillisecondType, DurationNanosecondType, DurationSecondType, Int16Type, Int32Type,
    Int64Type, Int8Type, SchemaRef, TimeUnit, UInt16Type, UInt32Type, UInt8Type,
};
use duckdb::arrow::ipc::writer::StreamWriter;
use lake_contracts::{partition_path, AssetClass, DataType, Date};
use serde_json::{Map, Number as JsonNumber, Value as Json};

use duckdb::{Connection, Error as DuckError};

/// The relation name the bounded engine exposes to user SQL.
const RELATION_NAME: &str = "data";

/// An invalid or over-budget bounded query.
///
/// Ports `lake.query.service.BoundedQueryError`: `code` is the stable
/// machine-readable rejection code the HTTP boundary maps onto status codes
/// (`scan_limit_exceeded`/`result_limit_exceeded` 413,
/// `force_override_not_authorized` 403, `query_storage_unavailable` 503, and
/// the validation rejections 422).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BoundedQueryError {
    /// Stable machine-readable rejection code.
    pub code: String,
    /// Human-readable failure message.
    pub message: String,
}

impl BoundedQueryError {
    /// Build an error with an explicit code and message.
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }

    fn sql_not_allowed(message: impl Into<String>) -> Self {
        Self::new("sql_not_allowed", message)
    }

    fn storage_path_not_allowed(message: impl Into<String>) -> Self {
        Self::new("storage_path_not_allowed", message)
    }

    fn scan_limit_exceeded(message: impl Into<String>) -> Self {
        Self::new("scan_limit_exceeded", message)
    }

    fn result_limit_exceeded(message: impl Into<String>) -> Self {
        Self::new("result_limit_exceeded", message)
    }

    fn query_storage_unavailable(message: impl Into<String>) -> Self {
        Self::new("query_storage_unavailable", message)
    }

    fn force_override_not_authorized(message: impl Into<String>) -> Self {
        Self::new("force_override_not_authorized", message)
    }

    fn internal(message: impl Into<String>) -> Self {
        Self::new("internal_error", message)
    }
}

impl std::fmt::Display for BoundedQueryError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for BoundedQueryError {}

/// Runtime byte limits for one bounded query.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct QueryLimits {
    /// The conservative catalog/file scan estimate cap.
    pub max_scan_bytes: u64,
    /// The serialized result cap.
    pub max_result_bytes: u64,
}

impl QueryLimits {
    /// Validate and build the limits, mirroring the Python `__post_init__`.
    ///
    /// Both limits must be positive and the result limit cannot exceed the
    /// scan limit; the messages are the Python `ValueError` texts.
    pub fn new(max_scan_bytes: u64, max_result_bytes: u64) -> Result<Self, String> {
        if max_scan_bytes == 0 || max_result_bytes == 0 {
            return Err("query byte limits must be positive".to_string());
        }
        if max_result_bytes > max_scan_bytes {
            return Err("result limit cannot exceed scan limit".to_string());
        }
        Ok(Self {
            max_scan_bytes,
            max_result_bytes,
        })
    }
}

/// One canonical catalog file made visible to the query relation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EnsuredQueryFile {
    /// Asset class of the partition.
    pub asset_class: AssetClass,
    /// Data type of the partition.
    pub data_type: DataType,
    /// Canonical uppercase symbol.
    pub symbol: String,
    /// Session the partition covers.
    pub session_date: Date,
    /// Canonical object path from the catalog.
    pub object_path: String,
    /// Catalog byte size of the object.
    pub byte_size: u64,
    /// Canonical bars interval token; bars files must carry it.
    pub interval: Option<String>,
}

impl EnsuredQueryFile {
    /// Build an ensured file, normalizing the symbol like the Python
    /// `__post_init__` (`strip().upper()`).
    pub fn new(
        asset_class: AssetClass,
        data_type: DataType,
        symbol: impl Into<String>,
        session_date: Date,
        object_path: impl Into<String>,
        byte_size: u64,
    ) -> Self {
        Self {
            asset_class,
            data_type,
            symbol: symbol.into().trim().to_uppercase(),
            session_date,
            object_path: object_path.into(),
            byte_size,
            interval: None,
        }
    }

    /// Set the canonical bars interval token for this file.
    #[must_use]
    pub fn with_interval(mut self, interval: Option<&str>) -> Self {
        self.interval = interval.map(str::to_string);
        self
    }

    /// Return the only object path valid for this request partition.
    pub fn expected_path(&self) -> Result<String, BoundedQueryError> {
        partition_path(
            self.session_date,
            self.asset_class,
            self.data_type,
            self.interval.as_deref(),
            &self.symbol,
        )
        .map_err(|error| BoundedQueryError::storage_path_not_allowed(error.to_string()))
    }
}

/// The representation requested for one bounded query result.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BoundedQueryOutput {
    /// JSON rows.
    Json,
    /// Arrow IPC stream bytes.
    Arrow,
}

/// JSON rows and Arrow IPC bytes produced by one bounded query.
#[derive(Clone, Debug, PartialEq)]
pub struct BoundedQueryResult {
    /// Column names in result order.
    pub columns: Vec<String>,
    /// JSON rows for JSON output; empty for Arrow output.
    pub rows: Vec<Map<String, Json>>,
    /// Arrow IPC stream bytes for Arrow output; empty for JSON output.
    pub arrow_bytes: Vec<u8>,
    /// Serialized result size in bytes.
    pub result_bytes: u64,
    /// Conservative catalog/file scan estimate in bytes.
    pub estimated_scan_bytes: u64,
    /// Whether the authorized force override was applied.
    pub forced: bool,
}

/// Execute SQL against only the supplied, canonical local Parquet files.
#[derive(Clone, Debug)]
pub struct BoundedDuckDbQuery {
    root: PathBuf,
    limits: QueryLimits,
}

impl BoundedDuckDbQuery {
    /// Build the executor over one resolved local query root.
    pub fn new(root: impl Into<PathBuf>, limits: QueryLimits) -> Self {
        Self {
            root: resolve_path(&root.into()),
            limits,
        }
    }

    /// The resolved local query root.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Run one bounded SELECT over the supplied ensured files.
    pub fn execute(
        &self,
        sql: &str,
        files: &[EnsuredQueryFile],
        force: bool,
        force_authorized: bool,
        output: BoundedQueryOutput,
    ) -> Result<BoundedQueryResult, BoundedQueryError> {
        let statement = validate_read_only_sql(sql)?;
        if force && !force_authorized {
            return Err(BoundedQueryError::force_override_not_authorized(
                "force override is not authorized for this query endpoint",
            ));
        }

        let (paths, estimated_scan_bytes) = self.resolve_files(files)?;
        if paths.is_empty() {
            return Err(BoundedQueryError::query_storage_unavailable(
                "no complete ensured files are available for this query",
            ));
        }
        if estimated_scan_bytes > self.limits.max_scan_bytes && !force {
            return Err(BoundedQueryError::scan_limit_exceeded(format!(
                "estimated scan of {estimated_scan_bytes} bytes exceeds the {}-byte limit",
                self.limits.max_scan_bytes
            )));
        }

        self.run(statement, &paths, force, output, estimated_scan_bytes)
    }

    /// Resolve the ensured files to local paths and their scan estimate.
    fn resolve_files(
        &self,
        files: &[EnsuredQueryFile],
    ) -> Result<(Vec<PathBuf>, u64), BoundedQueryError> {
        if !self.root.is_dir() {
            return Err(BoundedQueryError::query_storage_unavailable(
                "the local query root is unavailable",
            ));
        }

        let mut paths = Vec::new();
        let mut estimated_scan_bytes = 0u64;
        let mut seen = HashSet::new();
        for file in files {
            if file.object_path != file.expected_path()? {
                return Err(BoundedQueryError::storage_path_not_allowed(
                    "catalog object path does not match the canonical partition",
                ));
            }
            if file.object_path.is_empty()
                || file.object_path.contains('\\')
                || file.object_path.starts_with('/')
                || file
                    .object_path
                    .split('/')
                    .any(|part| part.is_empty() || part == "." || part == "..")
            {
                return Err(BoundedQueryError::storage_path_not_allowed(
                    "object path must be canonical",
                ));
            }
            let candidate = {
                let mut joined = self.root.clone();
                for part in file.object_path.split('/') {
                    joined.push(part);
                }
                joined
            };
            if candidate != self.root && !candidate.starts_with(&self.root) {
                return Err(BoundedQueryError::storage_path_not_allowed(
                    "object path escapes the query root",
                ));
            }
            if !seen.insert(candidate.clone()) {
                continue;
            }
            if !candidate.is_file() {
                return Err(BoundedQueryError::query_storage_unavailable(format!(
                    "ensured query file is unavailable: {}",
                    file.object_path
                )));
            }
            let canonical = std::fs::canonicalize(&candidate).map_err(|_| {
                BoundedQueryError::query_storage_unavailable(format!(
                    "ensured query file is unavailable: {}",
                    file.object_path
                ))
            })?;
            if canonical != self.root && !canonical.starts_with(&self.root) {
                return Err(BoundedQueryError::storage_path_not_allowed(
                    "object path escapes the query root",
                ));
            }
            let actual_size = std::fs::metadata(&candidate)
                .map(|metadata| metadata.len())
                .unwrap_or(0);
            estimated_scan_bytes += file.byte_size.max(actual_size);
            paths.push(candidate);
        }
        Ok((paths, estimated_scan_bytes))
    }

    /// Bind the relation, validate the plan, execute, and collect the result.
    fn run(
        &self,
        statement: String,
        paths: &[PathBuf],
        force: bool,
        output: BoundedQueryOutput,
        estimated_scan_bytes: u64,
    ) -> Result<BoundedQueryResult, BoundedQueryError> {
        let rejected = |error: DuckError| {
            BoundedQueryError::sql_not_allowed(format!("DuckDB rejected the SQL query: {error}"))
        };
        let connection = Connection::open_in_memory().map_err(rejected)?;
        let path_list = paths
            .iter()
            .map(|path| format!("'{}'", path.to_string_lossy().replace('\'', "''")))
            .collect::<Vec<_>>()
            .join(", ");
        // Materialize the ensured files into one relation while external
        // access is still enabled: after `enable_external_access = false`,
        // DuckDB refuses to re-bind a `read_parquet` view (and cannot even
        // autoload the extension), so the Python service's eagerly bound
        // `from_parquet(...).create("data")` maps to a temp table here.
        connection
            .execute_batch(&format!(
                "CREATE OR REPLACE TEMP TABLE {RELATION_NAME} AS \
                 SELECT * FROM read_parquet([{path_list}]);\n\
                 SET enable_external_access = false;"
            ))
            .map_err(rejected)?;

        validate_query_plan(&connection, &statement)?;
        let mut handle = connection.prepare(&statement).map_err(rejected)?;
        let mut result = handle.query_arrow([]).map_err(rejected)?;
        let schema = result.get_schema();
        let columns = schema
            .fields()
            .iter()
            .map(|field| field.name().clone())
            .collect::<Vec<_>>();
        let (rows, arrow_bytes, result_bytes) = match output {
            BoundedQueryOutput::Arrow => collect_arrow_result(
                result.by_ref(),
                &schema,
                self.limits.max_result_bytes,
                force,
            )?,
            BoundedQueryOutput::Json => collect_json_result(
                result.by_ref(),
                &schema,
                self.limits.max_result_bytes,
                force,
            )?,
        };

        Ok(BoundedQueryResult {
            columns,
            rows,
            arrow_bytes,
            result_bytes,
            estimated_scan_bytes,
            forced: force,
        })
    }
}

/// Validate one SELECT/CTE statement before giving it to DuckDB.
///
/// The connection intentionally exposes one materialized relation named
/// `data`. In addition to rejecting mutating statements, this lexical guard
/// rejects file table functions and direct path relations before DuckDB can
/// resolve them. The query plan is checked again after the relation is
/// installed.
pub fn validate_read_only_sql(sql: &str) -> Result<String, BoundedQueryError> {
    if sql.trim().is_empty() {
        return Err(BoundedQueryError::sql_not_allowed(
            "sql must be a non-empty read-only statement",
        ));
    }
    let tokens = tokenize_sql(sql)?;
    let Some(first) = tokens.first() else {
        return Err(BoundedQueryError::sql_not_allowed(
            "only SELECT statements are allowed",
        ));
    };
    if first.kind != TokenKind::Identifier || (first.value != "SELECT" && first.value != "WITH") {
        return Err(BoundedQueryError::sql_not_allowed(
            "only SELECT statements are allowed",
        ));
    }
    if tokens[..tokens.len() - 1]
        .iter()
        .any(|token| token.value == ";")
    {
        return Err(BoundedQueryError::sql_not_allowed(
            "multiple SQL statements are not allowed",
        ));
    }
    if tokens
        .iter()
        .any(|token| token.kind == TokenKind::Identifier && is_dangerous_keyword(&token.value))
    {
        return Err(BoundedQueryError::sql_not_allowed(
            "mutating or administrative SQL is not allowed",
        ));
    }

    for index in 0..tokens.len() - 1 {
        let token = &tokens[index];
        let next = &tokens[index + 1];
        if token.kind == TokenKind::Identifier && (token.value == "FROM" || token.value == "JOIN") {
            if next.kind == TokenKind::String {
                return Err(BoundedQueryError::storage_path_not_allowed(
                    "SQL cannot use a storage path as a relation",
                ));
            }
            if next.kind == TokenKind::Identifier && next.value == "LATERAL" {
                continue;
            }
        }
        if token.kind == TokenKind::Identifier
            && is_external_scan_function(&token.value)
            && next.value == "("
        {
            return Err(BoundedQueryError::storage_path_not_allowed(
                "SQL cannot invoke external storage table functions",
            ));
        }
    }

    Ok(sql.trim().to_string())
}

/// Check the bound query plan for external storage access.
fn validate_query_plan(connection: &Connection, sql: &str) -> Result<(), BoundedQueryError> {
    let rejected = |error: DuckError| {
        BoundedQueryError::sql_not_allowed(format!("DuckDB rejected the SQL query: {error}"))
    };
    let mut handle = connection
        .prepare(&format!("EXPLAIN {sql}"))
        .map_err(rejected)?;
    let mut rows = handle.query([]).map_err(rejected)?;
    let mut plan = String::new();
    while let Some(row) = rows.next().map_err(rejected)? {
        // The column count is only available after execution: reading it from
        // an unexecuted statement panics inside the duckdb crate.
        let column_count = row.as_ref().column_count();
        for column in 0..column_count {
            let value: String = row.get(column).map_err(rejected)?;
            plan.push_str(&value.to_uppercase());
            plan.push('\n');
        }
    }
    if EXTERNAL_SCAN_MARKERS
        .iter()
        .any(|marker| plan.contains(marker))
    {
        return Err(BoundedQueryError::storage_path_not_allowed(
            "query plan contains an external storage scan",
        ));
    }
    if !plan.contains("SEQ_SCAN") || !plan.contains(&RELATION_NAME.to_uppercase()) {
        return Err(BoundedQueryError::sql_not_allowed(
            "SQL must read the ensured data relation",
        ));
    }
    Ok(())
}

/// The collected result rows, encoded payload, and payload byte count.
type CollectedResult = (Vec<Map<String, Json>>, Vec<u8>, u64);

/// Serialize the arrow batches into one IPC stream under the result cap.
fn collect_arrow_result(
    result: &mut dyn Iterator<Item = RecordBatch>,
    schema: &SchemaRef,
    max_result_bytes: u64,
    force: bool,
) -> Result<CollectedResult, BoundedQueryError> {
    let encoded = |error: duckdb::arrow::error::ArrowError| {
        BoundedQueryError::internal(format!("arrow IPC encoding failed: {error}"))
    };
    let mut buffer = Vec::new();
    {
        let mut writer = StreamWriter::try_new(
            CountingWriter {
                inner: &mut buffer,
                written: 0,
            },
            schema,
        )
        .map_err(encoded)?;
        for batch in &mut *result {
            writer.write(&batch).map_err(encoded)?;
            if writer.get_ref().written > max_result_bytes && !force {
                return Err(BoundedQueryError::result_limit_exceeded(format!(
                    "result of more than {max_result_bytes} bytes exceeds the \
                     {max_result_bytes}-byte limit"
                )));
            }
        }
        // `into_inner` finishes the stream, writing the end-of-stream marker.
        writer.into_inner().map_err(encoded)?;
    }
    let result_bytes = buffer.len() as u64;
    check_result_size(result_bytes, max_result_bytes, force)?;
    Ok((Vec::new(), buffer, result_bytes))
}

/// A [`std::io::Write`] sink that forwards into a buffer while counting the
/// bytes it has written, so the result cap can be enforced mid-encoding.
struct CountingWriter<'a> {
    inner: &'a mut Vec<u8>,
    written: u64,
}

impl std::io::Write for CountingWriter<'_> {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let written = self.inner.write(buf)?;
        self.written += u64::try_from(written).unwrap_or(u64::MAX);
        Ok(written)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}

/// Render the arrow batches as JSON rows under the result cap.
fn collect_json_result(
    result: &mut dyn Iterator<Item = RecordBatch>,
    schema: &SchemaRef,
    max_result_bytes: u64,
    force: bool,
) -> Result<CollectedResult, BoundedQueryError> {
    let mut rows: Vec<Map<String, Json>> = Vec::new();
    let mut encoded_rows: Vec<Vec<u8>> = Vec::new();
    let mut result_bytes = 2u64;
    for batch in &mut *result {
        let batch_rows = record_batch_rows(&batch, schema)?;
        for row in batch_rows {
            let encoded = serde_json::to_vec(&Json::Object(row.clone()))
                .map_err(|error| BoundedQueryError::internal(error.to_string()))?;
            result_bytes += encoded.len() as u64 + u64::from(!rows.is_empty());
            check_result_size(result_bytes, max_result_bytes, force)?;
            rows.push(row);
            encoded_rows.push(encoded);
        }
    }
    let mut payload = Vec::with_capacity(result_bytes as usize);
    payload.push(b'[');
    for (index, encoded) in encoded_rows.iter().enumerate() {
        if index > 0 {
            payload.push(b',');
        }
        payload.extend_from_slice(encoded);
    }
    payload.push(b']');
    let payload_bytes = payload.len() as u64;
    check_result_size(payload_bytes, max_result_bytes, force)?;
    Ok((rows, Vec::new(), payload_bytes))
}

/// The Python `_check_result_size`: strict-greater rejections unless forced.
fn check_result_size(
    result_bytes: u64,
    max_result_bytes: u64,
    force: bool,
) -> Result<(), BoundedQueryError> {
    if result_bytes > max_result_bytes && !force {
        return Err(BoundedQueryError::result_limit_exceeded(format!(
            "result of {result_bytes} bytes exceeds the {max_result_bytes}-byte limit"
        )));
    }
    Ok(())
}

/// Render one RecordBatch into one JSON object per row.
fn record_batch_rows(
    batch: &RecordBatch,
    schema: &SchemaRef,
) -> Result<Vec<Map<String, Json>>, BoundedQueryError> {
    let mut rows = vec![Map::new(); batch.num_rows()];
    for field in schema.fields() {
        let name = field.name().clone();
        let Some(column) = batch.column_by_name(&name) else {
            return Err(BoundedQueryError::internal(format!(
                "missing column {name}"
            )));
        };
        let values = column_values(field.data_type(), column.as_ref())?;
        for (row, value) in rows.iter_mut().zip(values) {
            row.insert(name.clone(), value);
        }
    }
    Ok(rows)
}

/// Render one arrow column as one JSON value per row.
///
/// The mapping mirrors `pyarrow.Table.to_pylist` followed by
/// `pydantic_core.to_jsonable_python` and compact `orjson` serialization:
/// decimal128 columns render as the Python `str(Decimal)` text, timestamps
/// render as `datetime.isoformat()` strings, and lists and structs render
/// recursively.
fn column_values(data_type: &ArrowType, array: &dyn Array) -> Result<Vec<Json>, BoundedQueryError> {
    let values = match data_type {
        ArrowType::Null => vec![Json::Null; array.len()],
        ArrowType::Boolean => array
            .as_boolean()
            .into_iter()
            .map(|value| Json::Bool(value.unwrap_or_default()))
            .collect(),
        ArrowType::Int8 => signed_values::<Int8Type>(array),
        ArrowType::Int16 => signed_values::<Int16Type>(array),
        ArrowType::Int32 => signed_values::<Int32Type>(array),
        ArrowType::Int64 => primitive_values::<Int64Type, _>(array, Json::from),
        ArrowType::UInt8 => unsigned_values::<UInt8Type>(array),
        ArrowType::UInt16 => unsigned_values::<UInt16Type>(array),
        ArrowType::UInt32 => unsigned_values::<UInt32Type>(array),
        ArrowType::UInt64 => {
            primitive_values::<duckdb::arrow::datatypes::UInt64Type, _>(array, Json::from)
        }
        ArrowType::Float32 => {
            primitive_values::<duckdb::arrow::datatypes::Float32Type, _>(array, |value| {
                float_json(f64::from(value))
            })
        }
        ArrowType::Float64 => {
            primitive_values::<duckdb::arrow::datatypes::Float64Type, _>(array, float_json)
        }
        ArrowType::Decimal128(_, scale) => array
            .as_primitive::<Decimal128Type>()
            .into_iter()
            .map(|value| match value {
                Some(value) => Json::String(decimal_text(value, *scale)),
                None => Json::Null,
            })
            .collect(),
        ArrowType::Utf8 => string_values::<StringArray>(array),
        ArrowType::LargeUtf8 => string_values::<LargeStringArray>(array),
        ArrowType::Utf8View => array
            .as_string_view()
            .into_iter()
            .map(|value| value.map_or(Json::Null, |value| Json::String(value.to_string())))
            .collect(),
        ArrowType::Date32 => array
            .as_primitive::<Date32Type>()
            .into_iter()
            .map(|value| match value {
                Some(days) => Json::String(days_to_isoformat(i64::from(days))),
                None => Json::Null,
            })
            .collect(),
        ArrowType::Date64 => array
            .as_primitive::<Date64Type>()
            .into_iter()
            .map(|value| match value {
                Some(milliseconds) => {
                    Json::String(days_to_isoformat(milliseconds.div_euclid(86_400_000)))
                }
                None => Json::Null,
            })
            .collect(),
        ArrowType::Timestamp(unit, _) => timestamp_values(array, *unit),
        ArrowType::Time32(unit) | ArrowType::Time64(unit) => time_values(array, *unit),
        ArrowType::Duration(unit) => duration_values(array, *unit),
        ArrowType::List(_) => {
            let items = array.as_list::<i32>();
            let rendered = column_values(items.values().data_type(), items.values().as_ref())?;
            let offsets: Vec<i64> = items
                .value_offsets()
                .iter()
                .map(|offset| i64::from(*offset))
                .collect();
            list_rows(
                array,
                |index| offsets[index] as usize,
                |index| offsets[index + 1] as usize,
                &rendered,
            )
        }
        ArrowType::LargeList(_) => {
            let items = array.as_list::<i64>();
            let rendered = column_values(items.values().data_type(), items.values().as_ref())?;
            let offsets: Vec<usize> = items
                .value_offsets()
                .iter()
                .map(|offset| usize::try_from(*offset).unwrap_or(0))
                .collect();
            list_rows(
                array,
                |index| offsets[index],
                |index| offsets[index + 1],
                &rendered,
            )
        }
        ArrowType::FixedSizeList(_, width) => {
            let items = array.as_fixed_size_list();
            let stride = usize::try_from(*width)
                .map_err(|_| BoundedQueryError::internal("invalid fixed list width"))?;
            let rendered = column_values(items.values().data_type(), items.values().as_ref())?;
            (0..items.len())
                .map(|index| {
                    if items.is_null(index) {
                        Json::Null
                    } else {
                        Json::Array(rendered[index * stride..(index + 1) * stride].to_vec())
                    }
                })
                .collect()
        }
        ArrowType::Struct(_) => {
            let items = array.as_struct();
            let columns = items
                .fields()
                .iter()
                .zip(items.columns())
                .map(|(field, column)| {
                    column_values(field.data_type(), column.as_ref())
                        .map(|values| (field.name().clone(), values))
                })
                .collect::<Result<Vec<_>, BoundedQueryError>>()?;
            (0..items.len())
                .map(|index| {
                    if items.is_null(index) {
                        Json::Null
                    } else {
                        Json::Object(
                            columns
                                .iter()
                                .map(|(name, values)| (name.clone(), values[index].clone()))
                                .collect(),
                        )
                    }
                })
                .collect()
        }
        ArrowType::Map(_, _) => {
            let items = array.as_map();
            let entries = items.entries();
            let key_field = entries
                .fields()
                .first()
                .ok_or_else(|| BoundedQueryError::internal("map entries have no key field"))?;
            let value_field = entries
                .fields()
                .get(1)
                .ok_or_else(|| BoundedQueryError::internal("map entries have no value field"))?;
            let keys = column_values(key_field.data_type(), entries.column(0).as_ref())?;
            let entry_values = column_values(value_field.data_type(), entries.column(1).as_ref())?;
            (0..items.len())
                .map(|index| {
                    if items.is_null(index) {
                        Json::Null
                    } else {
                        let (start, end) = (
                            items.value_offsets()[index] as usize,
                            items.value_offsets()[index + 1] as usize,
                        );
                        Json::Array(
                            (start..end)
                                .map(|position| {
                                    Json::Array(vec![
                                        keys[position].clone(),
                                        entry_values[position].clone(),
                                    ])
                                })
                                .collect(),
                        )
                    }
                })
                .collect()
        }
        ArrowType::Dictionary(_, value_type) => {
            let (keys, values) = dictionary_parts(array)?;
            let rendered = column_values(value_type, values.as_ref())?;
            keys.iter()
                .map(|key| {
                    key.and_then(|key| usize::try_from(key).ok())
                        .and_then(|index| rendered.get(index).cloned())
                        .unwrap_or(Json::Null)
                })
                .collect()
        }
        other => {
            return Err(BoundedQueryError::internal(format!(
                "query result column type {other} is not JSON-renderable"
            )))
        }
    };
    Ok(values)
}

/// Render signed primitive columns as JSON integers.
fn signed_values<T>(array: &dyn Array) -> Vec<Json>
where
    T: duckdb::arrow::datatypes::ArrowPrimitiveType,
    T::Native: Into<i64>,
{
    primitive_values::<T, _>(array, |value| Json::from(value.into()))
}

/// Render unsigned primitive columns as JSON integers.
fn unsigned_values<T>(array: &dyn Array) -> Vec<Json>
where
    T: duckdb::arrow::datatypes::ArrowPrimitiveType,
    T::Native: Into<u64>,
{
    primitive_values::<T, _>(array, |value| Json::from(value.into()))
}

/// Render primitive values with a per-value JSON conversion.
fn primitive_values<T, F>(array: &dyn Array, render: F) -> Vec<Json>
where
    T: duckdb::arrow::datatypes::ArrowPrimitiveType,
    F: Fn(T::Native) -> Json,
{
    array
        .as_primitive::<T>()
        .into_iter()
        .map(|value| value.map_or(Json::Null, &render))
        .collect()
}

/// Render string values.
fn string_values<T: 'static>(array: &dyn Array) -> Vec<Json>
where
    for<'a> &'a T: IntoIterator<Item = Option<&'a str>>,
{
    let items = array.as_any().downcast_ref::<T>().expect("string array");
    items
        .into_iter()
        .map(|value| value.map_or(Json::Null, |value| Json::String(value.to_string())))
        .collect()
}

/// Render timestamp values as Python `datetime.isoformat()` strings.
///
/// The DuckDB session timezone is not localized in the Rust engine: a
/// timezone-carrying timestamp renders its UTC instant with a `+00:00`
/// offset, and a timezone-naive timestamp renders without an offset.
fn timestamp_values(array: &dyn Array, unit: TimeUnit) -> Vec<Json> {
    let render = |micros: i64| Json::String(isoformat_micros(micros, true));
    match unit {
        TimeUnit::Second => array
            .as_primitive::<duckdb::arrow::datatypes::TimestampSecondType>()
            .into_iter()
            .map(|value| value.map_or(Json::Null, |value| render(value.saturating_mul(1_000_000))))
            .collect(),
        TimeUnit::Millisecond => array
            .as_primitive::<duckdb::arrow::datatypes::TimestampMillisecondType>()
            .into_iter()
            .map(|value| value.map_or(Json::Null, |value| render(value.saturating_mul(1_000))))
            .collect(),
        TimeUnit::Microsecond => array
            .as_primitive::<duckdb::arrow::datatypes::TimestampMicrosecondType>()
            .into_iter()
            .map(|value| value.map_or(Json::Null, render))
            .collect(),
        TimeUnit::Nanosecond => array
            .as_primitive::<duckdb::arrow::datatypes::TimestampNanosecondType>()
            .into_iter()
            .map(|value| value.map_or(Json::Null, |value| render(value.div_euclid(1_000))))
            .collect(),
    }
}

/// Render time values as Python `time.isoformat()` strings.
fn time_values(array: &dyn Array, unit: TimeUnit) -> Vec<Json> {
    let render = |micros: i64| {
        let seconds = micros.div_euclid(1_000_000);
        let fraction = micros.rem_euclid(1_000_000);
        let mut text = format!(
            "{:02}:{:02}:{:02}",
            seconds / 3_600,
            (seconds % 3_600) / 60,
            seconds % 60
        );
        if fraction != 0 {
            text.push_str(&format!(".{fraction:06}"));
        }
        Json::String(text)
    };
    match unit {
        TimeUnit::Second => array
            .as_primitive::<duckdb::arrow::datatypes::Time32SecondType>()
            .into_iter()
            .map(|value| {
                value.map_or(Json::Null, |value| {
                    render(i64::from(value).saturating_mul(1_000_000))
                })
            })
            .collect(),
        TimeUnit::Millisecond => array
            .as_primitive::<duckdb::arrow::datatypes::Time32MillisecondType>()
            .into_iter()
            .map(|value| {
                value.map_or(Json::Null, |value| {
                    render(i64::from(value).saturating_mul(1_000))
                })
            })
            .collect(),
        TimeUnit::Microsecond => array
            .as_primitive::<duckdb::arrow::datatypes::Time64MicrosecondType>()
            .into_iter()
            .map(|value| value.map_or(Json::Null, render))
            .collect(),
        TimeUnit::Nanosecond => array
            .as_primitive::<duckdb::arrow::datatypes::Time64NanosecondType>()
            .into_iter()
            .map(|value| value.map_or(Json::Null, |value| render(value.div_euclid(1_000))))
            .collect(),
    }
}

/// Render duration values as Python timedelta float seconds.
fn duration_values(array: &dyn Array, unit: TimeUnit) -> Vec<Json> {
    let seconds = |value: i64| match unit {
        TimeUnit::Second => value as f64,
        TimeUnit::Millisecond => value as f64 / 1_000.0,
        TimeUnit::Microsecond => value as f64 / 1_000_000.0,
        TimeUnit::Nanosecond => value as f64 / 1_000_000_000.0,
    };
    match unit {
        TimeUnit::Second => {
            primitive_values::<DurationSecondType, _>(array, |value| float_json(seconds(value)))
        }
        TimeUnit::Millisecond => primitive_values::<DurationMillisecondType, _>(array, |value| {
            float_json(seconds(value))
        }),
        TimeUnit::Microsecond => primitive_values::<DurationMicrosecondType, _>(array, |value| {
            float_json(seconds(value))
        }),
        TimeUnit::Nanosecond => {
            primitive_values::<DurationNanosecondType, _>(array, |value| float_json(seconds(value)))
        }
    }
}

/// Render offset-based list rows from pre-rendered element values.
fn list_rows(
    outer: &dyn Array,
    start: impl Fn(usize) -> usize,
    end: impl Fn(usize) -> usize,
    rendered: &[Json],
) -> Vec<Json> {
    (0..outer.len())
        .map(|index| {
            if outer.is_null(index) {
                Json::Null
            } else {
                let (from, to) = (start(index), end(index));
                Json::Array(rendered[from..to.min(rendered.len())].to_vec())
            }
        })
        .collect()
}

/// The Python `str(Decimal)` rendering for a decimal128 value.
///
/// `pyarrow` materializes decimal columns as `decimal.Decimal` values whose
/// exponent is the negative column scale, and `to_jsonable_python` keeps them
/// as strings. The rendering follows the decimal `to-sci-string` rules: plain
/// notation while the adjusted exponent is at least -6, scientific notation
/// (`dE-18`) otherwise, with the column scale's trailing digits preserved.
fn decimal_text(unscaled: i128, scale: i8) -> String {
    let negative = unscaled < 0;
    let digits = unscaled.unsigned_abs().to_string();
    let exponent = -i64::from(scale);
    let adjusted = exponent + digits.len() as i64 - 1;
    let sign = if negative { "-" } else { "" };
    let scale = i64::from(scale);
    if exponent <= 0 && adjusted >= -6 {
        if scale == 0 {
            return format!("{sign}{digits}");
        }
        let scale = scale as usize;
        if digits.len() > scale {
            let point = digits.len() - scale;
            format!("{sign}{}.{}", &digits[..point], &digits[point..])
        } else {
            format!("{sign}0.{}{}", "0".repeat(scale - digits.len()), digits)
        }
    } else {
        let coefficient = if digits.len() == 1 {
            digits
        } else {
            format!("{}.{}", &digits[..1], &digits[1..])
        };
        if exponent < 0 {
            format!("{sign}{coefficient}E{exponent}")
        } else {
            format!("{sign}{coefficient}E+{exponent}")
        }
    }
}

/// Render a UTC microsecond instant the way Python renders a datetime.
fn isoformat_micros(micros: i64, aware: bool) -> String {
    let seconds = micros.div_euclid(1_000_000);
    let fraction = micros.rem_euclid(1_000_000);
    let days = seconds.div_euclid(86_400);
    let day_seconds = seconds.rem_euclid(86_400);
    let mut text = format!(
        "{}T{:02}:{:02}:{:02}",
        days_to_isoformat(days),
        day_seconds / 3_600,
        (day_seconds % 3_600) / 60,
        day_seconds % 60,
    );
    if fraction != 0 {
        text.push_str(&format!(".{fraction:06}"));
    }
    if aware {
        text.push_str("+00:00");
    }
    text
}

/// Render days since the Unix epoch the way Python renders a date.
fn days_to_isoformat(days: i64) -> String {
    // Howard Hinnant's civil_from_days algorithm.
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let shifted_month = (5 * day_of_year + 2) / 153;
    let month = if shifted_month < 10 {
        shifted_month + 3
    } else {
        shifted_month - 9
    };
    let day = day_of_year - (153 * shifted_month + 2) / 5 + 1;
    let year = if month <= 2 { year + 1 } else { year };
    format!("{year:04}-{month:02}-{day:02}")
}

/// Render a finite float the way orjson renders a Python float.
fn float_json(value: f64) -> Json {
    if value.is_finite() {
        JsonNumber::from_f64(value).map_or(Json::Null, Json::Number)
    } else {
        Json::Null
    }
}

/// Extract dictionary keys as i64 indices plus the values array, widening
/// narrower key types.
fn dictionary_parts(
    array: &dyn Array,
) -> Result<(Vec<Option<i64>>, duckdb::arrow::array::ArrayRef), BoundedQueryError> {
    macro_rules! key_indices {
        ($key_type:ty) => {{
            let items = array
                .as_any()
                .downcast_ref::<duckdb::arrow::array::DictionaryArray<$key_type>>()
                .ok_or_else(|| BoundedQueryError::internal("dictionary key type mismatch"))?;
            let values = items.values().clone();
            let keys = items
                .keys()
                .into_iter()
                .map(|key| key.and_then(|value| i64::try_from(value).ok()))
                .collect::<Vec<_>>();
            (keys, values)
        }};
    }
    match array.data_type() {
        ArrowType::Dictionary(key_type, _) => match key_type.as_ref() {
            ArrowType::Int8 => Ok(key_indices!(Int8Type)),
            ArrowType::Int16 => Ok(key_indices!(Int16Type)),
            ArrowType::Int32 => Ok(key_indices!(Int32Type)),
            ArrowType::Int64 => Ok(key_indices!(Int64Type)),
            ArrowType::UInt8 => Ok(key_indices!(duckdb::arrow::datatypes::UInt8Type)),
            ArrowType::UInt16 => Ok(key_indices!(duckdb::arrow::datatypes::UInt16Type)),
            ArrowType::UInt32 => Ok(key_indices!(duckdb::arrow::datatypes::UInt32Type)),
            ArrowType::UInt64 => Ok(key_indices!(duckdb::arrow::datatypes::UInt64Type)),
            other => Err(BoundedQueryError::internal(format!(
                "unsupported dictionary key type {other}"
            ))),
        },
        other => Err(BoundedQueryError::internal(format!(
            "unsupported dictionary key type {other}"
        ))),
    }
}

/// Resolve a path the way Python resolves one, tolerating absent targets.
fn resolve_path(path: &Path) -> PathBuf {
    std::fs::canonicalize(path).unwrap_or_else(|_| {
        let absolute = if path.is_absolute() {
            path.to_path_buf()
        } else {
            std::env::current_dir()
                .map(|current| current.join(path))
                .unwrap_or_else(|_| path.to_path_buf())
        };
        let mut resolved = PathBuf::new();
        for component in absolute.components() {
            match component {
                std::path::Component::CurDir => {}
                std::path::Component::ParentDir => {
                    resolved.pop();
                }
                other => resolved.push(other.as_os_str()),
            }
        }
        resolved
    })
}

/// One lexical SQL token.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct SqlToken {
    /// Token class: identifier, quoted identifier, string literal, or symbol.
    kind: TokenKind,
    /// Uppercased identifier text or the raw literal text.
    value: String,
}

/// The lexical SQL token classes the validator distinguishes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum TokenKind {
    /// A bare identifier, uppercased for keyword matching.
    Identifier,
    /// A double-quote or backtick quoted identifier.
    QuotedIdentifier,
    /// A single-quoted string literal.
    String,
    /// Any other single character.
    Symbol,
}

/// Reject mutating or administrative SQL keywords.
fn is_dangerous_keyword(value: &str) -> bool {
    matches!(
        value,
        "ALTER"
            | "ATTACH"
            | "BEGIN"
            | "CALL"
            | "CHECKPOINT"
            | "COMMIT"
            | "COPY"
            | "CREATE"
            | "CREDENTIAL"
            | "DELETE"
            | "DETACH"
            | "DROP"
            | "EXECUTE"
            | "EXPORT"
            | "GRANT"
            | "IMPORT"
            | "INSERT"
            | "INSTALL"
            | "LOAD"
            | "MACRO"
            | "MERGE"
            | "PREPARE"
            | "PRAGMA"
            | "RESET"
            | "REVOKE"
            | "ROLLBACK"
            | "SECRET"
            | "SEQUENCE"
            | "SET"
            | "TRUNCATE"
            | "UPDATE"
            | "USE"
            | "VACUUM"
    )
}

/// Plan markers that indicate an external storage scan.
const EXTERNAL_SCAN_MARKERS: &[&str] = &[
    "TABLE_FUNCTION",
    "READ_PARQUET",
    "PARQUET_SCAN",
    "READ_CSV",
    "CSV_SCAN",
    "READ_JSON",
    "JSON_SCAN",
    "HTTPFS",
    "HTTP_SCAN",
    "GENERATOR",
    "GENERATE_SERIES",
    "RANGE",
    "UNNEST",
];

/// Reject external storage table functions by name.
fn is_external_scan_function(value: &str) -> bool {
    matches!(
        value,
        "ARROW_SCAN"
            | "GLOB"
            | "HTTP_GET"
            | "PARQUET_SCAN"
            | "PARQUET_SCHEMA"
            | "PARQUET_METADATA"
            | "PARQUET_FILE_METADATA"
            | "PARQUET_KV_METADATA"
            | "QUERY_TABLE"
            | "READ_BLOB"
            | "READ_CSV"
            | "READ_CSV_AUTO"
            | "READ_JSON"
            | "READ_JSON_AUTO"
            | "READ_PARQUET"
            | "READ_TEXT"
            | "DELTA_SCAN"
            | "ICEBERG_SCAN"
            | "SQLITE_SCAN"
            | "POSTGRES_SCAN"
            | "MYSQL_SCAN"
            | "ODBC_SCAN"
    )
}

/// Tokenize one SQL statement the way the Python validator does.
fn tokenize_sql(sql: &str) -> Result<Vec<SqlToken>, BoundedQueryError> {
    let characters: Vec<char> = sql.chars().collect();
    let mut tokens = Vec::new();
    let mut index = 0usize;
    while index < characters.len() {
        let character = characters[index];
        if character.is_whitespace() {
            index += 1;
            continue;
        }
        if starts_with(&characters, index, "--") {
            index = match find_character(&characters, index + 2, '\n') {
                Some(position) => position + 1,
                None => characters.len(),
            };
            continue;
        }
        if starts_with(&characters, index, "/*") {
            let end = find_substring(&characters, index + 2, "*/")
                .ok_or_else(|| BoundedQueryError::sql_not_allowed("unterminated SQL comment"))?;
            index = end + 2;
            continue;
        }
        if character == '\'' {
            let (end, token) = scan_quoted(&characters, index, character)
                .ok_or_else(|| BoundedQueryError::sql_not_allowed("unterminated SQL string"))?;
            index = end;
            tokens.push(SqlToken {
                kind: TokenKind::String,
                value: token,
            });
            continue;
        }
        if character == '"' || character == '`' {
            let (end, token) = scan_quoted(&characters, index, character)
                .ok_or_else(|| BoundedQueryError::sql_not_allowed("unterminated SQL identifier"))?;
            index = end;
            tokens.push(SqlToken {
                kind: TokenKind::QuotedIdentifier,
                value: token,
            });
            continue;
        }
        if character.is_ascii_alphabetic() || character == '_' {
            let mut end = index + 1;
            while end < characters.len()
                && (characters[end].is_ascii_alphanumeric()
                    || characters[end] == '_'
                    || characters[end] == '$')
            {
                end += 1;
            }
            let value: String = characters[index..end].iter().collect();
            tokens.push(SqlToken {
                kind: TokenKind::Identifier,
                value: value.to_uppercase(),
            });
            index = end;
            continue;
        }
        tokens.push(SqlToken {
            kind: TokenKind::Symbol,
            value: character.to_string(),
        });
        index += 1;
    }
    Ok(tokens)
}

/// Whether `characters[index..]` starts with `prefix`.
fn starts_with(characters: &[char], index: usize, prefix: &str) -> bool {
    prefix.chars().enumerate().all(|(offset, expected)| {
        characters
            .get(index + offset)
            .is_some_and(|character| *character == expected)
    })
}

/// Find the first occurrence of one character at or after `index`.
fn find_character(characters: &[char], index: usize, expected: char) -> Option<usize> {
    characters
        .iter()
        .skip(index)
        .position(|character| *character == expected)
        .map(|position| position + index)
}

/// Find the first occurrence of a substring at or after `index`.
fn find_substring(characters: &[char], index: usize, needle: &str) -> Option<usize> {
    let needle: Vec<char> = needle.chars().collect();
    if needle.is_empty() || characters.len() < needle.len() {
        return None;
    }
    (index..=characters.len() - needle.len())
        .find(|start| characters[*start..*start + needle.len()] == needle[..])
}

/// Scan one quoted literal starting at `index`, returning the end and text.
fn scan_quoted(characters: &[char], index: usize, quote: char) -> Option<(usize, String)> {
    let start = index;
    let mut position = index + 1;
    while position < characters.len() {
        if characters[position] == quote {
            if characters.get(position + 1) == Some(&quote) {
                position += 2;
                continue;
            }
            return Some((
                position + 1,
                characters[start..position + 1].iter().collect(),
            ));
        }
        position += 1;
    }
    None
}

#[cfg(test)]
#[path = "../tests/unit/bounded.rs"]
mod tests;
