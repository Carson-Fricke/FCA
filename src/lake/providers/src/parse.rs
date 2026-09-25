//! Alpaca response page and row parsing.
//!
//! Ports the `_page_rows`/`_normalize_*` helpers of
//! `lake.providers.alpaca.client` and `records.py` field-for-field: the same
//! field ranges, UTC timestamps, exact decimals, conditions handling, and
//! protocol errors naming the offending field.

use lake_contracts::{DateTime, Decimal18};
use serde_json::Value;

use crate::error::ProviderError;
use crate::records::{
    NormalizedCryptoQuote, NormalizedCryptoTrade, NormalizedQuote, NormalizedTrade,
};

/// The two Alpaca data types the client fetches.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum DataType {
    /// Trade prints.
    Trades,
    /// Top-of-book quotes.
    Quotes,
}

impl DataType {
    /// The provider key and URL segment.
    pub(crate) const fn value(self) -> &'static str {
        match self {
            Self::Trades => "trades",
            Self::Quotes => "quotes",
        }
    }
}

/// Extract the rows and the next cursor from one equity or crypto page.
///
/// Equity pages may be keyed by symbol (`{"trades": {"AAPL": [...]}}`) or flat
/// (`{"trades": [...]}`); crypto pages are always flat. A missing key yields no
/// rows; a malformed shape is a protocol error.
pub(crate) fn page_rows<'a>(
    payload: &'a Value,
    data_type: DataType,
    symbol: &str,
) -> Result<(Vec<&'a Value>, Option<String>), ProviderError> {
    if !payload.is_object() {
        return Err(protocol("Alpaca response body must be an object"));
    }
    let data_type_name = data_type.value();
    let raw_rows = payload
        .get(data_type_name)
        .ok_or_else(|| protocol(format!("Alpaca response is missing '{data_type_name}'")))?;
    let symbol_rows = match raw_rows {
        Value::Object(rows_by_symbol) => rows_by_symbol.get(symbol),
        Value::Null => None,
        rows => Some(rows),
    };
    let rows = match symbol_rows {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(items)) => {
            let mut rows = Vec::with_capacity(items.len());
            for item in items {
                if !item.is_object() {
                    return Err(protocol(format!(
                        "Alpaca {data_type_name} row must be an object"
                    )));
                }
                rows.push(item);
            }
            rows
        }
        Some(_) => {
            return Err(protocol(format!(
                "Alpaca {data_type_name} rows must be an array"
            )))
        }
    };
    let next_page_token = match payload.get("next_page_token") {
        None | Some(Value::Null) => None,
        Some(Value::String(token)) => Some(token.clone()),
        Some(_) => {
            return Err(protocol("Alpaca next_page_token must be a string or null"));
        }
    };
    Ok((rows, next_page_token))
}

/// Parse one equity trade row.
pub(crate) fn parse_trade(row: &Value, symbol: &str) -> Result<NormalizedTrade, ProviderError> {
    let update = match row.get("u") {
        None | Some(Value::Null) => None,
        Some(Value::String(text)) if !text.is_empty() => Some(text.clone()),
        Some(_) => {
            return Err(protocol("Alpaca trade update must be a non-empty string"));
        }
    };
    Ok(NormalizedTrade {
        timestamp: timestamp(row)?,
        symbol: symbol.to_string(),
        trade_id: u64::try_from(integer(row, "i", 0, Some(i128::from(u64::MAX)))?)
            .expect("bounded by the maximum"),
        exchange: text(row, "x")?,
        price: decimal(row, "p")?,
        size: u32::try_from(integer(row, "s", 0, Some(i128::from(u32::MAX)))?)
            .expect("bounded by the maximum"),
        conditions: conditions(row)?,
        tape: text(row, "z")?,
        update,
    })
}

/// Parse one equity quote row.
pub(crate) fn parse_quote(row: &Value, symbol: &str) -> Result<NormalizedQuote, ProviderError> {
    Ok(NormalizedQuote {
        timestamp: timestamp(row)?,
        symbol: symbol.to_string(),
        bid_exchange: text(row, "bx")?,
        bid_price: decimal(row, "bp")?,
        bid_size: u32::try_from(integer(row, "bs", 0, Some(i128::from(u32::MAX)))?)
            .expect("bounded by the maximum"),
        ask_exchange: text(row, "ax")?,
        ask_price: decimal(row, "ap")?,
        ask_size: u32::try_from(integer(row, "as", 0, Some(i128::from(u32::MAX)))?)
            .expect("bounded by the maximum"),
        conditions: conditions(row)?,
        tape: text(row, "z")?,
    })
}

/// Parse one crypto trade row.
pub(crate) fn parse_crypto_trade(
    row: &Value,
    symbol: &str,
) -> Result<NormalizedCryptoTrade, ProviderError> {
    Ok(NormalizedCryptoTrade {
        timestamp: timestamp(row)?,
        symbol: symbol.to_string(),
        trade_id: optional_integer(row, "i", 0, Some(i128::from(i64::MAX)))?
            .map(|value| value as i64),
        exchange: optional_text(row, "x")?,
        price: decimal(row, "p")?,
        size: decimal(row, "s")?,
        taker_side: optional_text(row, "tks")?,
    })
}

/// Parse one crypto quote row.
pub(crate) fn parse_crypto_quote(
    row: &Value,
    symbol: &str,
) -> Result<NormalizedCryptoQuote, ProviderError> {
    Ok(NormalizedCryptoQuote {
        timestamp: timestamp(row)?,
        symbol: symbol.to_string(),
        bid_exchange: optional_text(row, "bx")?,
        bid_price: decimal(row, "bp")?,
        bid_size: decimal(row, "bs")?,
        ask_exchange: optional_text(row, "ax")?,
        ask_price: decimal(row, "ap")?,
        ask_size: decimal(row, "as")?,
    })
}

fn required<'a>(row: &'a Value, key: &str) -> Result<&'a Value, ProviderError> {
    row.get(key)
        .ok_or_else(|| protocol(format!("Alpaca row is missing '{key}'")))
}

fn text(row: &Value, key: &str) -> Result<String, ProviderError> {
    match required(row, key)? {
        Value::String(text) if !text.is_empty() => Ok(text.clone()),
        _ => Err(protocol(format!(
            "Alpaca field '{key}' must be a non-empty string"
        ))),
    }
}

fn optional_text(row: &Value, key: &str) -> Result<Option<String>, ProviderError> {
    match row.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) if !text.is_empty() => Ok(Some(text.clone())),
        Some(_) => Err(protocol(format!(
            "Alpaca field '{key}' must be a non-empty string"
        ))),
    }
}

fn integer(
    row: &Value,
    key: &str,
    minimum: i128,
    maximum: Option<i128>,
) -> Result<i128, ProviderError> {
    let value = required(row, key)?;
    let number = match value {
        Value::Number(number) => number
            .as_i64()
            .map(i128::from)
            .or_else(|| number.as_u64().map(i128::from)),
        _ => None,
    };
    let Some(number) = number else {
        return Err(protocol(format!("Alpaca field '{key}' must be an integer")));
    };
    if number < minimum || maximum.is_some_and(|limit| number > limit) {
        return Err(protocol(format!("Alpaca field '{key}' must be an integer")));
    }
    Ok(number)
}

fn optional_integer(
    row: &Value,
    key: &str,
    minimum: i128,
    maximum: Option<i128>,
) -> Result<Option<i128>, ProviderError> {
    match row.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(_) => integer(row, key, minimum, maximum).map(Some),
    }
}

fn decimal(row: &Value, key: &str) -> Result<Decimal18, ProviderError> {
    let value = required(row, key)?;
    let text = match value {
        Value::String(text) => text.clone(),
        Value::Number(number) => number.to_string(),
        _ => return Err(numeric_error(key)),
    };
    let text = text.trim();
    if is_non_finite(text) {
        return Err(protocol(format!("Alpaca field '{key}' must be finite")));
    }
    Decimal18::parse(text).map_err(|_| numeric_error(key))
}

fn numeric_error(key: &str) -> ProviderError {
    protocol(format!("Alpaca field '{key}' must be numeric"))
}

fn is_non_finite(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    let body = lower
        .strip_prefix('+')
        .or_else(|| lower.strip_prefix('-'))
        .unwrap_or(lower.as_str());
    body == "nan"
        || body == "inf"
        || body == "infinity"
        || (body.starts_with("nan") && body[3..].bytes().all(|byte| byte.is_ascii_digit()))
}

fn timestamp(row: &Value) -> Result<DateTime, ProviderError> {
    let value = text(row, "t")?;
    if !has_timezone(&value) {
        return Err(protocol("Alpaca timestamp must include a timezone"));
    }
    DateTime::parse_iso_utc(&value).ok_or_else(|| protocol("Alpaca timestamp must be ISO-8601"))
}

fn has_timezone(value: &str) -> bool {
    value.ends_with('Z')
        || value.rfind('+').is_some_and(|index| index > 10)
        || value.rfind('-').is_some_and(|index| index > 10)
}

fn conditions(row: &Value) -> Result<Vec<String>, ProviderError> {
    let Value::Array(items) = required(row, "c")? else {
        return Err(protocol("Alpaca conditions must be an array"));
    };
    let mut conditions = Vec::with_capacity(items.len());
    for item in items {
        let Value::String(condition) = item else {
            return Err(protocol("Alpaca conditions must contain strings"));
        };
        conditions.push(condition.clone());
    }
    Ok(conditions)
}

fn protocol(message: impl Into<String>) -> ProviderError {
    ProviderError::Protocol(message.into())
}

#[cfg(test)]
#[path = "../tests/unit/parse.rs"]
mod tests;
