//! Exact typed provider records projected directly into pinned Arrow columns.
//!
//! Transactions validate and order borrowed records before projection. No JSON,
//! decimal text conversion, or per-column thread pools sit on the ingest path.

use std::sync::Arc;

use arrow_array::builder::{ListBuilder, StringBuilder};
use arrow_array::{
    ArrayRef, Decimal128Array, Int64Array, RecordBatch, StringArray, TimestampNanosecondArray,
    UInt32Array, UInt64Array,
};
use arrow_schema::{DataType as ArrowType, Field};
use lake_contracts::{schema_for, AssetClass, DataError, DataType, DateTime, Decimal18};
use lake_providers::{
    NormalizedBar, NormalizedCryptoQuote, NormalizedCryptoTrade, NormalizedOptionQuote,
    NormalizedOptionTrade, NormalizedQuote, NormalizedTrade,
};

fn timestamps(values: impl Iterator<Item = DateTime>) -> ArrayRef {
    Arc::new(
        TimestampNanosecondArray::from_iter_values(values.map(DateTime::to_nanos))
            .with_timezone("UTC"),
    )
}

fn decimals(values: impl Iterator<Item = Decimal18>) -> Result<ArrayRef, DataError> {
    let values = Decimal128Array::from_iter_values(values.map(Decimal18::unscaled))
        .with_precision_and_scale(38, 18)
        .map_err(DataError::io)?;
    Ok(Arc::new(values))
}

fn strings<'a>(values: impl Iterator<Item = &'a str>) -> ArrayRef {
    Arc::new(StringArray::from_iter_values(values))
}

fn optional_strings<'a>(values: impl Iterator<Item = Option<&'a str>>) -> ArrayRef {
    Arc::new(StringArray::from_iter(values))
}

fn symbols(symbol: &str, rows: usize) -> ArrayRef {
    strings(std::iter::repeat_n(symbol, rows))
}

fn conditions<'a>(values: impl Iterator<Item = &'a Vec<String>>) -> ArrayRef {
    let mut builder = ListBuilder::new(StringBuilder::new()).with_field(Arc::new(Field::new(
        "item",
        ArrowType::Utf8,
        false,
    )));
    for conditions in values {
        for condition in conditions {
            builder.values().append_value(condition);
        }
        builder.append(true);
    }
    Arc::new(builder.finish())
}

fn batch(
    asset: AssetClass,
    data_type: DataType,
    columns: Vec<ArrayRef>,
) -> Result<RecordBatch, DataError> {
    RecordBatch::try_new(Arc::new(schema_for(asset, data_type)?), columns).map_err(DataError::io)
}

pub(crate) fn equity_trades(
    records: &[&NormalizedTrade],
    symbol: &str,
) -> Result<RecordBatch, DataError> {
    batch(
        AssetClass::Equity,
        DataType::Trade,
        vec![
            timestamps(records.iter().map(|r| r.timestamp)),
            symbols(symbol, records.len()),
            Arc::new(UInt64Array::from_iter_values(
                records.iter().map(|r| r.trade_id),
            )),
            strings(records.iter().map(|r| r.exchange.as_str())),
            decimals(records.iter().map(|r| r.price))?,
            Arc::new(UInt32Array::from_iter_values(
                records.iter().map(|r| r.size),
            )),
            conditions(records.iter().map(|r| &r.conditions)),
            strings(records.iter().map(|r| r.tape.as_str())),
            optional_strings(records.iter().map(|r| r.update.as_deref())),
        ],
    )
}

pub(crate) fn equity_quotes(
    records: &[&NormalizedQuote],
    symbol: &str,
) -> Result<RecordBatch, DataError> {
    batch(
        AssetClass::Equity,
        DataType::Quote,
        vec![
            timestamps(records.iter().map(|r| r.timestamp)),
            symbols(symbol, records.len()),
            strings(records.iter().map(|r| r.bid_exchange.as_str())),
            decimals(records.iter().map(|r| r.bid_price))?,
            Arc::new(UInt32Array::from_iter_values(
                records.iter().map(|r| r.bid_size),
            )),
            strings(records.iter().map(|r| r.ask_exchange.as_str())),
            decimals(records.iter().map(|r| r.ask_price))?,
            Arc::new(UInt32Array::from_iter_values(
                records.iter().map(|r| r.ask_size),
            )),
            conditions(records.iter().map(|r| &r.conditions)),
            strings(records.iter().map(|r| r.tape.as_str())),
        ],
    )
}

pub(crate) fn option_trades(
    records: &[&NormalizedOptionTrade],
    symbol: &str,
) -> Result<RecordBatch, DataError> {
    batch(
        AssetClass::Option,
        DataType::Trade,
        vec![
            timestamps(records.iter().map(|r| r.timestamp)),
            symbols(symbol, records.len()),
            strings(records.iter().map(|r| r.exchange.as_str())),
            decimals(records.iter().map(|r| r.price))?,
            Arc::new(UInt32Array::from_iter_values(
                records.iter().map(|r| r.size),
            )),
            strings(records.iter().map(|r| r.condition.as_str())),
        ],
    )
}

pub(crate) fn option_quotes(
    records: &[&NormalizedOptionQuote],
    symbol: &str,
) -> Result<RecordBatch, DataError> {
    batch(
        AssetClass::Option,
        DataType::Quote,
        vec![
            timestamps(records.iter().map(|r| r.timestamp)),
            symbols(symbol, records.len()),
            strings(records.iter().map(|r| r.bid_exchange.as_str())),
            decimals(records.iter().map(|r| r.bid_price))?,
            Arc::new(UInt32Array::from_iter_values(
                records.iter().map(|r| r.bid_size),
            )),
            strings(records.iter().map(|r| r.ask_exchange.as_str())),
            decimals(records.iter().map(|r| r.ask_price))?,
            Arc::new(UInt32Array::from_iter_values(
                records.iter().map(|r| r.ask_size),
            )),
            strings(records.iter().map(|r| r.condition.as_str())),
        ],
    )
}

pub(crate) fn crypto_trades(
    records: &[&NormalizedCryptoTrade],
    symbol: &str,
) -> Result<RecordBatch, DataError> {
    batch(
        AssetClass::Crypto,
        DataType::Trade,
        vec![
            timestamps(records.iter().map(|r| r.timestamp)),
            symbols(symbol, records.len()),
            Arc::new(Int64Array::from_iter(records.iter().map(|r| r.trade_id))),
            optional_strings(records.iter().map(|r| r.exchange.as_deref())),
            decimals(records.iter().map(|r| r.price))?,
            decimals(records.iter().map(|r| r.size))?,
            optional_strings(records.iter().map(|r| r.taker_side.as_deref())),
        ],
    )
}

pub(crate) fn crypto_quotes(
    records: &[&NormalizedCryptoQuote],
    symbol: &str,
) -> Result<RecordBatch, DataError> {
    batch(
        AssetClass::Crypto,
        DataType::Quote,
        vec![
            timestamps(records.iter().map(|r| r.timestamp)),
            symbols(symbol, records.len()),
            optional_strings(records.iter().map(|r| r.bid_exchange.as_deref())),
            decimals(records.iter().map(|r| r.bid_price))?,
            decimals(records.iter().map(|r| r.bid_size))?,
            optional_strings(records.iter().map(|r| r.ask_exchange.as_deref())),
            decimals(records.iter().map(|r| r.ask_price))?,
            decimals(records.iter().map(|r| r.ask_size))?,
        ],
    )
}

pub(crate) fn bars(
    records: &[NormalizedBar],
    asset: AssetClass,
    symbol: &str,
) -> Result<RecordBatch, DataError> {
    batch(
        asset,
        DataType::Bars,
        vec![
            timestamps(records.iter().map(|r| r.timestamp)),
            symbols(symbol, records.len()),
            decimals(records.iter().map(|r| r.open))?,
            decimals(records.iter().map(|r| r.high))?,
            decimals(records.iter().map(|r| r.low))?,
            decimals(records.iter().map(|r| r.close))?,
            decimals(records.iter().map(|r| r.volume))?,
        ],
    )
}
