//! Asset classes, data types, and the frozen Hive partition layout.
//!
//! Object layout, file naming, and partition paths are frozen: the harness
//! reads `date=/asset_class=/data_type=/SYMBOL.parquet` partitions directly
//! from disk.

use core::fmt;

use crate::error::DataError;

/// Stable lake asset classes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum AssetClass {
    /// Listed equities.
    Equity,
    /// Listed options.
    Option,
    /// Crypto pairs.
    Crypto,
    /// Reference data.
    Reference,
}

impl AssetClass {
    /// The persisted string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::Equity => "equity",
            Self::Option => "option",
            Self::Crypto => "crypto",
            Self::Reference => "reference",
        }
    }

    /// Parse the persisted string value.
    pub fn from_value(value: &str) -> Option<Self> {
        match value {
            "equity" => Some(Self::Equity),
            "option" => Some(Self::Option),
            "crypto" => Some(Self::Crypto),
            "reference" => Some(Self::Reference),
            _ => None,
        }
    }
}

impl fmt::Display for AssetClass {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.value())
    }
}

/// Stable lake data types.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum DataType {
    /// Executed trades.
    Trade,
    /// Best bid/offer quotes.
    Quote,
    /// Reference classification snapshots.
    Classification,
    /// Reference fundamentals facts.
    Fundamentals,
}

impl DataType {
    /// The persisted string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::Trade => "trade",
            Self::Quote => "quote",
            Self::Classification => "classification",
            Self::Fundamentals => "fundamentals",
        }
    }

    /// Parse the persisted string value.
    pub fn from_value(value: &str) -> Option<Self> {
        match value {
            "trade" => Some(Self::Trade),
            "quote" => Some(Self::Quote),
            "classification" => Some(Self::Classification),
            "fundamentals" => Some(Self::Fundamentals),
            _ => None,
        }
    }
}

impl fmt::Display for DataType {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.value())
    }
}

/// A supported `(asset class, data type)` dataset.
pub type Dataset = (AssetClass, DataType);

/// The datasets with pinned Arrow schemas, in registry order.
pub const SUPPORTED_DATASETS: &[Dataset] = &[
    (AssetClass::Equity, DataType::Trade),
    (AssetClass::Equity, DataType::Quote),
    (AssetClass::Option, DataType::Trade),
    (AssetClass::Option, DataType::Quote),
    (AssetClass::Crypto, DataType::Trade),
    (AssetClass::Crypto, DataType::Quote),
];

const SYMBOL_PATTERN_FIRST: fn(char) -> bool = |ch| ch.is_ascii_uppercase() || ch.is_ascii_digit();
const SYMBOL_PATTERN_REST: fn(char) -> bool =
    |ch| ch.is_ascii_uppercase() || ch.is_ascii_digit() || "._/-".contains(ch);

/// Canonicalise `symbol` and percent-encode it for use as a Parquet filename.
pub fn symbol_filename(symbol: &str) -> Result<String, DataError> {
    let canonical = symbol.trim().to_uppercase();
    if canonical.is_empty()
        || canonical == "."
        || canonical == ".."
        || !canonical.chars().all(SYMBOL_PATTERN_REST)
        || !canonical.chars().next().is_some_and(SYMBOL_PATTERN_FIRST)
    {
        return Err(DataError::value(format!(
            "invalid market symbol: {symbol:?}"
        )));
    }
    let mut encoded = String::with_capacity(canonical.len() + 8);
    for byte in canonical.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-') {
            encoded.push(byte as char);
        } else {
            encoded.push('%');
            encoded.push_str(&format!("{byte:02X}"));
        }
    }
    Ok(format!("{encoded}.parquet"))
}

/// Build a repository-independent, date-first Hive object path.
pub fn partition_path(
    session_date: crate::time::Date,
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
) -> Result<String, DataError> {
    Ok(format!(
        "date={}/asset_class={}/data_type={}/{}",
        session_date.isoformat(),
        asset_class.value(),
        data_type.value(),
        symbol_filename(symbol)?
    ))
}

#[cfg(test)]
#[path = "../tests/unit/partitions.rs"]
mod tests;
