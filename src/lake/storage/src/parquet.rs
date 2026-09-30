//! Idempotent, schema-preserving Parquet partition writes.
//!
//! Ports `lake.storage.parquet`. Records are normalized into the pinned Arrow
//! schema from `lake_contracts::schema_for`, serialized as one zstd-compressed
//! Parquet object with `PARQUET_2_0` metadata, and committed through the
//! whole-object [`BlobStore`] boundary. Row order is preserved.

use std::sync::Arc;

use arrow_array::{
    ArrayRef, Decimal128Array, Int64Array, ListArray, RecordBatch, StringArray,
    TimestampNanosecondArray, UInt32Array, UInt64Array,
};
use arrow_buffer::{OffsetBuffer, ScalarBuffer};
use arrow_schema::{DataType as ArrowType, Field, TimeUnit};
use lake_contracts::schemas::{SCHEMA_METADATA_KEY, SCHEMA_VERSION_METADATA_KEY};
use lake_contracts::{
    partition_path, schema_for, symbol_filename, AssetClass, DataError, DataType, Date, DateTime,
    Decimal18,
};
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::metadata::KeyValue;
use parquet::file::properties::{WriterProperties, WriterVersion};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::ports::BlobStore;

/// Metadata returned for the exact bytes committed to an object store.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ParquetMetadata {
    /// The repository-relative object path.
    pub path: String,
    /// The number of rows in the object.
    pub rows: u64,
    /// The payload size in bytes.
    pub bytes: u64,
    /// The lowercase sha256 hex digest of the payload.
    pub checksum: String,
}

impl ParquetMetadata {
    /// The repository-relative object path.
    pub fn object_path(&self) -> &str {
        &self.path
    }
}

/// Serialize normalized records and replace their deterministic object.
pub struct ParquetWriter<S: BlobStore> {
    store: Arc<S>,
}

impl<S: BlobStore + 'static> ParquetWriter<S> {
    /// Build a writer over a shared object store.
    pub fn new(store: Arc<S>) -> Self {
        Self { store }
    }

    /// Write one zstd-compressed Parquet object and return its metadata.
    ///
    /// `interval` carries the bars interval partition dimension; every other
    /// data type must pass `None`, and bars must pass a canonical token so the
    /// frozen `.../data_type=bars/interval=<token>/SYMBOL.parquet` path is
    /// produced.
    pub fn write(
        &self,
        records: &[Value],
        session_date: Date,
        asset_class: AssetClass,
        data_type: DataType,
        interval: Option<&str>,
        symbol: &str,
    ) -> Result<ParquetMetadata, DataError> {
        let normalize_started = std::time::Instant::now();
        let batch = normalize_records(records, asset_class, data_type, symbol)?;
        let normalize_seconds = normalize_started.elapsed().as_secs_f64();
        let schema = batch.schema();
        let metadata = schema.metadata();
        let name = metadata
            .get(SCHEMA_METADATA_KEY)
            .cloned()
            .unwrap_or_default();
        let version = metadata
            .get(SCHEMA_VERSION_METADATA_KEY)
            .cloned()
            .unwrap_or_default();
        let properties = WriterProperties::builder()
            .set_compression(Compression::ZSTD(Default::default()))
            .set_writer_version(WriterVersion::PARQUET_2_0)
            .set_key_value_metadata(Some(vec![
                KeyValue::new(SCHEMA_METADATA_KEY.to_string(), name),
                KeyValue::new(SCHEMA_VERSION_METADATA_KEY.to_string(), version),
            ]))
            .build();
        let mut payload = Vec::new();
        let encode_started = std::time::Instant::now();
        {
            let mut writer = ArrowWriter::try_new(&mut payload, schema, Some(properties))
                .map_err(DataError::io)?;
            writer.write(&batch).map_err(DataError::io)?;
            writer.close().map_err(DataError::io)?;
        }
        let encode_seconds = encode_started.elapsed().as_secs_f64();
        let path = partition_path(session_date, asset_class, data_type, interval, symbol)?;
        let checksum_started = std::time::Instant::now();
        let checksum = hex::encode(Sha256::digest(&payload));
        self.store.replace(&path, &payload)?;
        eprintln!(
            "PROFILE parquet rows={} normalize_seconds={normalize_seconds:.3} encode_seconds={encode_seconds:.3} checksum_store_seconds={:.3} bytes={}",
            batch.num_rows(),
            checksum_started.elapsed().as_secs_f64(),
            payload.len()
        );
        Ok(ParquetMetadata {
            path,
            rows: batch.num_rows() as u64,
            bytes: payload.len() as u64,
            checksum,
        })
    }
}

/// Normalize records into the stable Arrow schema for one partition.
///
/// The partition symbol is authoritative. Input timestamps become UTC
/// nanoseconds and decimal fields accept exact decimal or integer values, but
/// not binary floating-point values. Unknown record keys are ignored and row
/// order is preserved.
pub fn normalize_records(
    records: &[Value],
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
) -> Result<RecordBatch, DataError> {
    let schema = schema_for(asset_class, data_type)?;
    let canonical_symbol = canonical_partition_symbol(symbol)?;
    for (row, record) in records.iter().enumerate() {
        if !record.is_object() {
            return Err(DataError::type_error(format!(
                "record {row} must be an object"
            )));
        }
        validate_row_symbol(record, &canonical_symbol, row)?;
    }
    let columns = normalized_columns(&schema, records, &canonical_symbol)?;
    RecordBatch::try_new(Arc::new(schema), columns)
        .map_err(|error| DataError::value(error.to_string()))
}

/// Build every Arrow column in parallel; column order and row order are
/// preserved exactly, only wall time changes. Column construction is the
/// dominant cost for million-row partitions (one full pass per field), and the
/// fields are independent, so scoped OS threads parallelize it without
/// touching the async runtime.
fn normalized_columns(
    schema: &arrow_schema::Schema,
    records: &[Value],
    canonical_symbol: &str,
) -> Result<Vec<ArrayRef>, DataError> {
    std::thread::scope(|scope| {
        let handles: Vec<_> = schema
            .fields()
            .iter()
            .map(|field| scope.spawn(move || normalized_column(field, records, canonical_symbol)))
            .collect();
        let mut columns = Vec::with_capacity(handles.len());
        for handle in handles {
            let column = handle
                .join()
                .map_err(|_| DataError::value("column worker panicked"))?;
            columns.push(column?);
        }
        Ok(columns)
    })
}

fn normalized_column(
    field: &Field,
    records: &[Value],
    canonical_symbol: &str,
) -> Result<ArrayRef, DataError> {
    let name = field.name().as_str();
    if name == "symbol" {
        return Ok(Arc::new(symbol_column(records, canonical_symbol)));
    }
    match field.data_type() {
        ArrowType::Timestamp(TimeUnit::Nanosecond, _) => {
            let mut values = Vec::with_capacity(records.len());
            for (row, record) in records.iter().enumerate() {
                values.push(timestamp_nanoseconds(
                    required_value(record, field, row)?,
                    name,
                )?);
            }
            Ok(Arc::new(
                TimestampNanosecondArray::from(values).with_timezone("UTC"),
            ))
        }
        ArrowType::Decimal128(precision, scale) => {
            let mut values = Vec::with_capacity(records.len());
            for (row, record) in records.iter().enumerate() {
                values.push(decimal_value(required_value(record, field, row)?, name)?.unscaled());
            }
            let array = Decimal128Array::from(values)
                .with_precision_and_scale(*precision, *scale)
                .map_err(|error| DataError::value(error.to_string()))?;
            Ok(Arc::new(array))
        }
        ArrowType::UInt32 => {
            let mut values = Vec::with_capacity(records.len());
            for (row, record) in records.iter().enumerate() {
                let value = unsigned_value(required_value(record, field, row)?, name)?;
                values.push(u32::try_from(value).map_err(|_| out_of_range(name))?);
            }
            Ok(Arc::new(UInt32Array::from(values)))
        }
        ArrowType::UInt64 => {
            let mut values = Vec::with_capacity(records.len());
            for (row, record) in records.iter().enumerate() {
                let value = unsigned_value(required_value(record, field, row)?, name)?;
                values.push(u64::try_from(value).map_err(|_| out_of_range(name))?);
            }
            Ok(Arc::new(UInt64Array::from(values)))
        }
        ArrowType::Int64 => {
            let mut values = Vec::with_capacity(records.len());
            for record in records {
                match value_for(record, name) {
                    Some(value) => {
                        values.push(Some(
                            i64::try_from(integer_value(value, name)?)
                                .map_err(|_| out_of_range(name))?,
                        ));
                    }
                    None => values.push(None),
                }
            }
            Ok(Arc::new(Int64Array::from(values)))
        }
        ArrowType::Utf8 => {
            let mut values = Vec::with_capacity(records.len());
            for (row, record) in records.iter().enumerate() {
                let value = if field.is_nullable() {
                    value_for(record, name)
                } else {
                    Some(required_value(record, field, row)?)
                };
                match value {
                    Some(value) => values.push(Some(string_value(value, name)?.to_string())),
                    None => values.push(None),
                }
            }
            Ok(Arc::new(StringArray::from(values)))
        }
        ArrowType::List(item) if item.data_type() == &ArrowType::Utf8 => {
            Ok(Arc::new(string_list_column(field, item, records)?))
        }
        other => Err(DataError::Unsupported(format!(
            "unsupported lake field type {other}"
        ))),
    }
}

fn symbol_column(records: &[Value], canonical_symbol: &str) -> StringArray {
    let values = vec![canonical_symbol.to_string(); records.len()];
    StringArray::from(values)
}

fn validate_row_symbol(
    record: &Value,
    canonical_symbol: &str,
    row: usize,
) -> Result<(), DataError> {
    match record.get("symbol") {
        None | Some(Value::Null) => Ok(()),
        Some(Value::String(input)) => {
            if canonical_partition_symbol(input)? == canonical_symbol {
                Ok(())
            } else {
                Err(DataError::value(format!(
                    "record {row} symbol does not match partition symbol"
                )))
            }
        }
        Some(_) => Err(DataError::type_error(format!(
            "record {row} symbol must be a string"
        ))),
    }
}

fn string_list_column(
    field: &Field,
    item: &Field,
    records: &[Value],
) -> Result<ListArray, DataError> {
    let name = field.name().as_str();
    let mut offsets = Vec::with_capacity(records.len() + 1);
    offsets.push(0i32);
    let mut values: Vec<String> = Vec::new();
    for (row, record) in records.iter().enumerate() {
        let list = list_values(required_value(record, field, row)?, name)?;
        for value in list {
            values.push(value.to_string());
        }
        offsets.push(i32::try_from(values.len()).map_err(|_| out_of_range(name))?);
    }
    Ok(ListArray::new(
        Arc::new(item.clone()),
        OffsetBuffer::new(ScalarBuffer::from(offsets)),
        Arc::new(StringArray::from(values)),
        None,
    ))
}

fn value_for<'a>(record: &'a Value, name: &str) -> Option<&'a Value> {
    record
        .as_object()
        .and_then(|object| object.get(name))
        .filter(|value| !value.is_null())
}

fn required_value<'a>(
    record: &'a Value,
    field: &Field,
    row: usize,
) -> Result<&'a Value, DataError> {
    value_for(record, field.name()).ok_or_else(|| {
        DataError::value(format!(
            "record {row} is missing required field '{}'",
            field.name()
        ))
    })
}

fn canonical_partition_symbol(symbol: &str) -> Result<String, DataError> {
    let canonical = symbol.trim().to_uppercase();
    symbol_filename(&canonical)?;
    Ok(canonical)
}

fn integer_value(value: &Value, field_name: &str) -> Result<i128, DataError> {
    if let Value::Number(number) = value {
        if let Some(integer) = number.as_i64() {
            return Ok(i128::from(integer));
        }
        if let Some(integer) = number.as_u64() {
            return Ok(i128::from(integer));
        }
    }
    Err(DataError::type_error(format!(
        "{field_name} must be an integer"
    )))
}

fn unsigned_value(value: &Value, field_name: &str) -> Result<i128, DataError> {
    let integer = integer_value(value, field_name)?;
    if integer < 0 {
        return Err(DataError::value(format!(
            "{field_name} must be a non-negative integer"
        )));
    }
    Ok(integer)
}

fn string_value<'a>(value: &'a Value, field_name: &str) -> Result<&'a str, DataError> {
    value
        .as_str()
        .ok_or_else(|| DataError::type_error(format!("{field_name} must be a string")))
}

fn list_values<'a>(value: &'a Value, field_name: &str) -> Result<Vec<&'a str>, DataError> {
    let items = value
        .as_array()
        .ok_or_else(|| DataError::type_error(format!("{field_name} must be a list or tuple")))?;
    items
        .iter()
        .map(|item| {
            item.as_str().ok_or_else(|| {
                DataError::type_error(format!("{field_name} must contain only strings"))
            })
        })
        .collect()
}

fn decimal_value(value: &Value, field_name: &str) -> Result<Decimal18, DataError> {
    if let Value::String(text) = value {
        return parse_decimal(text, field_name);
    }
    let Value::Number(number) = value else {
        return Err(DataError::type_error(format!(
            "{field_name} must be an exact decimal value, not float"
        )));
    };
    if let Some(integer) = number.as_i64() {
        return parse_decimal(&integer.to_string(), field_name);
    }
    if let Some(integer) = number.as_u64() {
        return parse_decimal(&integer.to_string(), field_name);
    }
    Err(DataError::type_error(format!(
        "{field_name} must be an exact decimal value, not float"
    )))
}

fn parse_decimal(text: &str, field_name: &str) -> Result<Decimal18, DataError> {
    Decimal18::parse(text)
        .map_err(|_| DataError::value(format!("{field_name} must be an exact decimal value")))
}

fn timestamp_nanoseconds(value: &Value, field_name: &str) -> Result<i64, DataError> {
    if let Value::String(text) = value {
        return iso_nanoseconds(text, field_name);
    }
    let Value::Number(number) = value else {
        return Err(timestamp_type_error(field_name));
    };
    if let Some(integer) = number.as_i64() {
        return Ok(integer);
    }
    if let Some(integer) = number.as_u64() {
        return i64::try_from(integer).map_err(|_| out_of_range(field_name));
    }
    Err(timestamp_type_error(field_name))
}

fn timestamp_type_error(field_name: &str) -> DataError {
    DataError::type_error(format!(
        "{field_name} must be an aware datetime or UTC nanoseconds"
    ))
}

fn iso_nanoseconds(text: &str, field_name: &str) -> Result<i64, DataError> {
    let parsed = DateTime::parse_iso_utc(text)
        .ok_or_else(|| DataError::value(format!("{field_name} must be an ISO-8601 timestamp")))?;
    let sub_micros = parsed.micros().rem_euclid(1_000_000);
    let mut nanoseconds = parsed.to_nanos();
    if let Some(fraction) = fraction_nanoseconds(text) {
        nanoseconds += fraction - sub_micros * 1_000;
    }
    Ok(nanoseconds)
}

fn fraction_nanoseconds(text: &str) -> Option<i64> {
    if !text.is_ascii() {
        return None;
    }
    let body = strip_terminal_offset(text)?;
    let dot = body.rfind('.')?;
    let digits = &body[dot + 1..];
    if digits.is_empty() || digits.len() > 9 || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let mut value = 0i64;
    for index in 0..9 {
        let digit = digits
            .as_bytes()
            .get(index)
            .map_or(0, |byte| i64::from(byte - b'0'));
        value = value * 10 + digit;
    }
    Some(value)
}

fn strip_terminal_offset(text: &str) -> Option<&str> {
    if let Some(body) = text.strip_suffix('Z') {
        return Some(body);
    }
    if text.len() >= 6 && is_long_offset(&text[text.len() - 6..]) {
        return Some(&text[..text.len() - 6]);
    }
    if text.len() >= 5 && is_short_offset(&text[text.len() - 5..]) {
        return Some(&text[..text.len() - 5]);
    }
    None
}

fn is_short_offset(tail: &str) -> bool {
    let bytes = tail.as_bytes();
    bytes.len() == 5
        && (bytes[0] == b'+' || bytes[0] == b'-')
        && bytes[1..].iter().all(u8::is_ascii_digit)
}

fn is_long_offset(tail: &str) -> bool {
    let bytes = tail.as_bytes();
    bytes.len() == 6
        && (bytes[0] == b'+' || bytes[0] == b'-')
        && bytes[1..3].iter().all(u8::is_ascii_digit)
        && bytes[3] == b':'
        && bytes[4..6].iter().all(u8::is_ascii_digit)
}

fn out_of_range(field_name: &str) -> DataError {
    DataError::value(format!(
        "{field_name} is outside the persisted integer range"
    ))
}
