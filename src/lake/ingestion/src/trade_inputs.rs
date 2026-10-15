//! Pure Arrow-to-domain projections for bar derivation; reading/verification belongs to harness-market-reader.
use arrow_array::{
    Array, Decimal128Array, Int64Array, ListArray, RecordBatch, StringArray,
    TimestampNanosecondArray, UInt32Array, UInt64Array,
};
use lake_contracts::{DataError, DateTime, Decimal18};
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

pub fn equity_trades(batches: &[RecordBatch]) -> Result<Vec<EquityTradeRecord>, DataError> {
    let mut records = Vec::new();
    for batch in batches {
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
    Ok(records)
}

/// Read one `crypto.trade` partition into exact typed records.
pub fn crypto_trades(batches: &[RecordBatch]) -> Result<Vec<CryptoTradeRecord>, DataError> {
    let mut records = Vec::new();
    for batch in batches {
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
    Ok(records)
}

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
