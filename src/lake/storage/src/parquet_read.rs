//! Typed Parquet partition reads for derivation inputs.
//!
//! Derived bars need the stored trade partitions back as exact decimal records
//! instead of ad-hoc JSON or raw Arrow arrays spread across call sites. This
//! module reads one stored trade object through the read boundary and returns
//! the pinned typed records plus the object's content revision: the same
//! lowercase sha256 the writer records in coverage, recomputed from the exact
//! bytes so a derivation can record which trade bytes produced it.

use arrow_array::{
    Array, Decimal128Array, Int64Array, ListArray, RecordBatch, StringArray,
    TimestampNanosecondArray, UInt32Array, UInt64Array,
};
use arrow_schema::Schema;
use bytes::Bytes;
use lake_contracts::schemas::SCHEMA_METADATA_KEY;
use lake_contracts::{DataError, DateTime, Decimal18};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use sha2::{Digest, Sha256};

use crate::ports::ReadableBlobStore;

/// One stored equity trade restored from an `equity.trade` partition.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EquityTradeRecord {
    /// Trade time in UTC.
    pub timestamp: DateTime,
    /// Canonical symbol stored in the partition.
    pub symbol: String,
    /// Provider trade identifier.
    pub trade_id: u64,
    /// Exchange code.
    pub exchange: String,
    /// Exact trade price at scale 18.
    pub price: Decimal18,
    /// Share size.
    pub size: u32,
    /// Sale conditions in stored order.
    pub conditions: Vec<String>,
    /// Tape.
    pub tape: String,
    /// Update marker; absent when the provider omitted it.
    pub update: Option<String>,
}

/// One stored crypto trade restored from a `crypto.trade` partition.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CryptoTradeRecord {
    /// Trade time in UTC.
    pub timestamp: DateTime,
    /// Canonical pair stored in the partition.
    pub symbol: String,
    /// Provider trade identifier; absent when the provider omitted it.
    pub trade_id: Option<i64>,
    /// Exchange code; absent when the provider omitted it.
    pub exchange: Option<String>,
    /// Exact trade price at scale 18.
    pub price: Decimal18,
    /// Exact fractional size at scale 18.
    pub size: Decimal18,
    /// Taker side; absent when the provider omitted it.
    pub taker_side: Option<String>,
}

/// Restored records plus the sha256 revision of the exact stored bytes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TradePartition<T> {
    /// Typed records in stored row order.
    pub records: Vec<T>,
    /// Lowercase sha256 of the partition payload; the derivation revision.
    pub revision: String,
}

/// Read one `equity.trade` partition into exact typed records.
pub fn read_equity_trades(
    store: &dyn ReadableBlobStore,
    relative_path: &str,
) -> Result<TradePartition<EquityTradeRecord>, DataError> {
    let (batches, revision) = read_batches(store, relative_path, "equity.trade")?;
    let mut records = Vec::new();
    for batch in &batches {
        for row in 0..batch.num_rows() {
            records.push(EquityTradeRecord {
                timestamp: timestamp(batch, "timestamp", row)?,
                symbol: string(batch, "symbol", row)?,
                trade_id: column::<UInt64Array>(batch, "trade_id")?.value(row),
                exchange: string(batch, "exchange", row)?,
                price: decimal(batch, "price", row)?,
                size: column::<UInt32Array>(batch, "size")?.value(row),
                conditions: string_list(batch, "conditions", row)?,
                tape: string(batch, "tape", row)?,
                update: optional_string(batch, "update", row)?,
            });
        }
    }
    Ok(TradePartition { records, revision })
}

/// Read one `crypto.trade` partition into exact typed records.
pub fn read_crypto_trades(
    store: &dyn ReadableBlobStore,
    relative_path: &str,
) -> Result<TradePartition<CryptoTradeRecord>, DataError> {
    let (batches, revision) = read_batches(store, relative_path, "crypto.trade")?;
    let mut records = Vec::new();
    for batch in &batches {
        for row in 0..batch.num_rows() {
            records.push(CryptoTradeRecord {
                timestamp: timestamp(batch, "timestamp", row)?,
                symbol: string(batch, "symbol", row)?,
                trade_id: optional_i64(batch, "trade_id", row)?,
                exchange: optional_string(batch, "exchange", row)?,
                price: decimal(batch, "price", row)?,
                size: decimal(batch, "size", row)?,
                taker_side: optional_string(batch, "taker_side", row)?,
            });
        }
    }
    Ok(TradePartition { records, revision })
}

/// Read every record batch from one partition and its content revision.
fn read_batches(
    store: &dyn ReadableBlobStore,
    relative_path: &str,
    expected_dataset: &str,
) -> Result<(Vec<RecordBatch>, String), DataError> {
    let payload = store.read(relative_path)?;
    let revision = hex::encode(Sha256::digest(&payload));
    let builder =
        ParquetRecordBatchReaderBuilder::try_new(Bytes::from(payload)).map_err(DataError::io)?;
    let dataset = builder
        .metadata()
        .file_metadata()
        .key_value_metadata()
        .and_then(|entries| {
            entries
                .iter()
                .find(|entry| entry.key == SCHEMA_METADATA_KEY)
                .and_then(|entry| entry.value.clone())
        });
    if dataset.as_deref() != Some(expected_dataset) {
        return Err(DataError::value(format!(
            "stored partition is not a {expected_dataset} object"
        )));
    }
    let reader = builder.build().map_err(DataError::io)?;
    let mut batches = Vec::new();
    for batch in reader {
        batches.push(batch.map_err(DataError::io)?);
    }
    Ok((batches, revision))
}

/// Visit every decoded record batch of one stored partition in file order.
///
/// The payload must decode against `expected_schema`; any deviation is
/// rejected before a batch is handed to the visitor, so a caller streaming a
/// frozen dataset stream never re-derives the schema from the object. Batches
/// are decoded lazily, one at a time, which bounds memory to a single file
/// payload plus one decoded batch.
pub fn visit_partition_batches(
    store: &dyn ReadableBlobStore,
    relative_path: &str,
    expected_schema: &Schema,
    mut visit: impl FnMut(RecordBatch) -> Result<(), DataError>,
) -> Result<(), DataError> {
    let payload = store.read(relative_path)?;
    let builder =
        ParquetRecordBatchReaderBuilder::try_new(Bytes::from(payload)).map_err(DataError::io)?;
    if !fields_equivalent(builder.schema().as_ref(), expected_schema) {
        return Err(DataError::value(format!(
            "stored partition has an unexpected schema: {relative_path}"
        )));
    }
    let reader = builder.build().map_err(DataError::io)?;
    for batch in reader {
        visit(batch.map_err(DataError::io)?)?;
    }
    Ok(())
}

/// Whether two schemas carry the same fields in the same order, ignoring
/// metadata.
fn fields_equivalent(actual: &Schema, expected: &Schema) -> bool {
    actual.fields().len() == expected.fields().len()
        && actual
            .fields()
            .iter()
            .zip(expected.fields().iter())
            .all(|(actual, expected)| {
                actual.name() == expected.name()
                    && actual.data_type() == expected.data_type()
                    && actual.is_nullable() == expected.is_nullable()
            })
}

/// Downcast one named column of a batch, naming the column on type mismatch.
fn column<'a, T: 'static>(batch: &'a RecordBatch, name: &str) -> Result<&'a T, DataError> {
    let index = batch
        .schema()
        .index_of(name)
        .map_err(|_| DataError::value(format!("stored partition is missing '{name}'")))?;
    batch
        .column(index)
        .as_any()
        .downcast_ref::<T>()
        .ok_or_else(|| {
            DataError::type_error(format!("stored field '{name}' has an unexpected type"))
        })
}

/// Read one required non-empty string value.
fn string(batch: &RecordBatch, name: &str, row: usize) -> Result<String, DataError> {
    Ok(column::<StringArray>(batch, name)?.value(row).to_string())
}

/// Read one nullable string value.
fn optional_string(
    batch: &RecordBatch,
    name: &str,
    row: usize,
) -> Result<Option<String>, DataError> {
    let values = column::<StringArray>(batch, name)?;
    if values.is_null(row) {
        Ok(None)
    } else {
        Ok(Some(values.value(row).to_string()))
    }
}

/// Read one nullable integer value.
fn optional_i64(batch: &RecordBatch, name: &str, row: usize) -> Result<Option<i64>, DataError> {
    let values = column::<Int64Array>(batch, name)?;
    if values.is_null(row) {
        Ok(None)
    } else {
        Ok(Some(values.value(row)))
    }
}

/// Read one UTC nanosecond timestamp value.
fn timestamp(batch: &RecordBatch, name: &str, row: usize) -> Result<DateTime, DataError> {
    let values = column::<TimestampNanosecondArray>(batch, name)?;
    Ok(DateTime::from_nanos(values.value(row)))
}

/// Read one `decimal128(38, 18)` value as an exact [`Decimal18`].
fn decimal(batch: &RecordBatch, name: &str, row: usize) -> Result<Decimal18, DataError> {
    let values = column::<Decimal128Array>(batch, name)?;
    if values.precision() != 38 || values.scale() != 18 {
        return Err(DataError::type_error(format!(
            "stored field '{name}' is not decimal128(38, 18)"
        )));
    }
    Decimal18::from_unscaled(values.value(row)).map_err(DataError::from)
}

/// Read one `list<utf8>` value in stored order.
fn string_list(batch: &RecordBatch, name: &str, row: usize) -> Result<Vec<String>, DataError> {
    let list = column::<ListArray>(batch, name)?;
    let offsets = list.value_offsets();
    let start = usize::try_from(offsets[row])
        .map_err(|_| DataError::value(format!("stored field '{name}' has an invalid offset")))?;
    let end = usize::try_from(offsets[row + 1])
        .map_err(|_| DataError::value(format!("stored field '{name}' has an invalid offset")))?;
    let items = list
        .values()
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(|| {
            DataError::type_error(format!("stored field '{name}' must contain strings"))
        })?;
    let mut values = Vec::with_capacity(end - start);
    for index in start..end {
        values.push(items.value(index).to_string());
    }
    Ok(values)
}
