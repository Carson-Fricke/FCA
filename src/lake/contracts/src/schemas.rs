//! Versioned Arrow schemas for persisted market trades and quotes.
//!
//! Fields normalize Alpaca SIP, OPRA, and crypto payload names while preserving
//! their native precision and identifiers. Increment [`SCHEMA_VERSION`] for any
//! field, type, order, nullability, or semantic change.

use std::collections::HashMap;
use std::sync::Arc;

use arrow_schema::{DataType as ArrowType, Field, Schema, TimeUnit};

use crate::error::DataError;
use crate::partitions::{AssetClass, DataType};

/// The pinned lake schema version.
pub const SCHEMA_VERSION: &str = "1";

/// The metadata key carrying the dataset name (`equity.trade`).
pub const SCHEMA_METADATA_KEY: &str = "lake.schema";

/// The metadata key carrying the schema version.
pub const SCHEMA_VERSION_METADATA_KEY: &str = "lake.schema.version";

fn timestamp() -> ArrowType {
    ArrowType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()))
}

fn price() -> ArrowType {
    ArrowType::Decimal128(38, 18)
}

fn conditions() -> ArrowType {
    ArrowType::List(Arc::new(Field::new("item", ArrowType::Utf8, false)))
}

fn required(name: &str, data_type: ArrowType) -> Field {
    Field::new(name, data_type, false)
}

fn nullable(name: &str, data_type: ArrowType) -> Field {
    Field::new(name, data_type, true)
}

fn with_metadata(name: &str, fields: Vec<Field>) -> Schema {
    let mut metadata = HashMap::new();
    metadata.insert(SCHEMA_METADATA_KEY.to_string(), name.to_string());
    metadata.insert(
        SCHEMA_VERSION_METADATA_KEY.to_string(),
        SCHEMA_VERSION.to_string(),
    );
    Schema::new_with_metadata(fields, metadata)
}

/// `equity.trade`.
pub fn equity_trade_schema() -> Schema {
    with_metadata(
        "equity.trade",
        vec![
            required("timestamp", timestamp()),
            required("symbol", ArrowType::Utf8),
            required("trade_id", ArrowType::UInt64),
            required("exchange", ArrowType::Utf8),
            required("price", price()),
            required("size", ArrowType::UInt32),
            required("conditions", conditions()),
            required("tape", ArrowType::Utf8),
            nullable("update", ArrowType::Utf8),
        ],
    )
}

/// `equity.quote`.
pub fn equity_quote_schema() -> Schema {
    with_metadata(
        "equity.quote",
        vec![
            required("timestamp", timestamp()),
            required("symbol", ArrowType::Utf8),
            required("bid_exchange", ArrowType::Utf8),
            required("bid_price", price()),
            required("bid_size", ArrowType::UInt32),
            required("ask_exchange", ArrowType::Utf8),
            required("ask_price", price()),
            required("ask_size", ArrowType::UInt32),
            required("conditions", conditions()),
            required("tape", ArrowType::Utf8),
        ],
    )
}

/// `option.trade`.
pub fn option_trade_schema() -> Schema {
    with_metadata(
        "option.trade",
        vec![
            required("timestamp", timestamp()),
            required("symbol", ArrowType::Utf8),
            required("exchange", ArrowType::Utf8),
            required("price", price()),
            required("size", ArrowType::UInt32),
            required("condition", ArrowType::Utf8),
        ],
    )
}

/// `option.quote`.
pub fn option_quote_schema() -> Schema {
    with_metadata(
        "option.quote",
        vec![
            required("timestamp", timestamp()),
            required("symbol", ArrowType::Utf8),
            required("bid_exchange", ArrowType::Utf8),
            required("bid_price", price()),
            required("bid_size", ArrowType::UInt32),
            required("ask_exchange", ArrowType::Utf8),
            required("ask_price", price()),
            required("ask_size", ArrowType::UInt32),
            required("condition", ArrowType::Utf8),
        ],
    )
}

/// `crypto.trade`.
pub fn crypto_trade_schema() -> Schema {
    with_metadata(
        "crypto.trade",
        vec![
            required("timestamp", timestamp()),
            required("symbol", ArrowType::Utf8),
            nullable("trade_id", ArrowType::Int64),
            nullable("exchange", ArrowType::Utf8),
            required("price", price()),
            required("size", price()),
            nullable("taker_side", ArrowType::Utf8),
        ],
    )
}

/// `crypto.quote`.
pub fn crypto_quote_schema() -> Schema {
    with_metadata(
        "crypto.quote",
        vec![
            required("timestamp", timestamp()),
            required("symbol", ArrowType::Utf8),
            nullable("bid_exchange", ArrowType::Utf8),
            required("bid_price", price()),
            required("bid_size", price()),
            nullable("ask_exchange", ArrowType::Utf8),
            required("ask_price", price()),
            required("ask_size", price()),
        ],
    )
}

/// `equity.bars`.
pub fn equity_bars_schema() -> Schema {
    with_metadata(
        "equity.bars",
        vec![
            required("timestamp", timestamp()),
            required("symbol", ArrowType::Utf8),
            required("open", price()),
            required("high", price()),
            required("low", price()),
            required("close", price()),
            required("volume", price()),
        ],
    )
}

/// `crypto.bars`.
pub fn crypto_bars_schema() -> Schema {
    with_metadata(
        "crypto.bars",
        vec![
            required("timestamp", timestamp()),
            required("symbol", ArrowType::Utf8),
            required("open", price()),
            required("high", price()),
            required("low", price()),
            required("close", price()),
            required("volume", price()),
        ],
    )
}

/// The dataset name carried in schema metadata (`equity.trade`).
pub fn schema_name(asset_class: AssetClass, data_type: DataType) -> Option<&'static str> {
    match (asset_class, data_type) {
        (AssetClass::Equity, DataType::Trade) => Some("equity.trade"),
        (AssetClass::Equity, DataType::Quote) => Some("equity.quote"),
        (AssetClass::Option, DataType::Trade) => Some("option.trade"),
        (AssetClass::Option, DataType::Quote) => Some("option.quote"),
        (AssetClass::Crypto, DataType::Trade) => Some("crypto.trade"),
        (AssetClass::Crypto, DataType::Quote) => Some("crypto.quote"),
        (AssetClass::Equity, DataType::Bars) => Some("equity.bars"),
        (AssetClass::Crypto, DataType::Bars) => Some("crypto.bars"),
        _ => None,
    }
}

/// Return the stable schema for a supported asset and data type.
pub fn schema_for(asset_class: AssetClass, data_type: DataType) -> Result<Schema, DataError> {
    match (asset_class, data_type) {
        (AssetClass::Equity, DataType::Trade) => Ok(equity_trade_schema()),
        (AssetClass::Equity, DataType::Quote) => Ok(equity_quote_schema()),
        (AssetClass::Option, DataType::Trade) => Ok(option_trade_schema()),
        (AssetClass::Option, DataType::Quote) => Ok(option_quote_schema()),
        (AssetClass::Crypto, DataType::Trade) => Ok(crypto_trade_schema()),
        (AssetClass::Crypto, DataType::Quote) => Ok(crypto_quote_schema()),
        (AssetClass::Equity, DataType::Bars) => Ok(equity_bars_schema()),
        (AssetClass::Crypto, DataType::Bars) => Ok(crypto_bars_schema()),
        _ => Err(DataError::Unsupported(format!(
            "no lake schema for {}.{}",
            asset_class, data_type
        ))),
    }
}

#[cfg(test)]
#[path = "../tests/unit/schemas.rs"]
mod tests;
