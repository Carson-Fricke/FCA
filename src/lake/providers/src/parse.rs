//! Alpaca response page and row parsing.
//!
//! Ports the `_page_rows`/`_normalize_*` helpers of
//! `lake.providers.alpaca.client` and `records.py` field-for-field: the same
//! field ranges, UTC timestamps, exact decimals, conditions handling, and
//! protocol errors naming the offending field.

use lake_catalog::ContractRecord;
use lake_contracts::{DateTime, Decimal18};
use serde::Deserialize;
use serde_json::value::RawValue;
use smol_str::SmolStr;
use std::collections::BTreeMap;

use crate::error::ProviderError;
use crate::records::{
    NormalizedBar, NormalizedCryptoQuote, NormalizedCryptoTrade, NormalizedOptionQuote,
    NormalizedOptionTrade, NormalizedQuote, NormalizedTrade,
};

/// The three Alpaca data types the client fetches.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum DataType {
    /// Trade prints.
    Trades,
    /// Top-of-book quotes.
    Quotes,
    /// Session-aligned OHLCV bars.
    Bars,
}

impl DataType {
    /// The provider key and URL segment.
    pub(crate) const fn value(self) -> &'static str {
        match self {
            Self::Trades => "trades",
            Self::Quotes => "quotes",
            Self::Bars => "bars",
        }
    }
}

/// Borrowed wire fields; JSON values are validated directly into normalized records.
#[derive(Deserialize)]
pub(crate) struct RawRow<'a> {
    #[serde(skip)]
    parsed_nanos: std::cell::OnceCell<Option<i64>>,
    #[serde(borrow)]
    t: Option<&'a RawValue>,
    #[serde(borrow)]
    i: Option<&'a RawValue>,
    #[serde(borrow)]
    x: Option<&'a RawValue>,
    #[serde(borrow)]
    p: Option<&'a RawValue>,
    #[serde(borrow)]
    s: Option<&'a RawValue>,
    #[serde(borrow)]
    c: Option<&'a RawValue>,
    #[serde(borrow)]
    z: Option<&'a RawValue>,
    #[serde(borrow)]
    u: Option<&'a RawValue>,
    #[serde(borrow)]
    bx: Option<&'a RawValue>,
    #[serde(borrow)]
    bp: Option<&'a RawValue>,
    #[serde(borrow)]
    bs: Option<&'a RawValue>,
    #[serde(borrow)]
    ax: Option<&'a RawValue>,
    #[serde(borrow)]
    ap: Option<&'a RawValue>,
    #[serde(borrow, rename = "as")]
    ask_size_wire: Option<&'a RawValue>,
    #[serde(borrow)]
    tks: Option<&'a RawValue>,
    #[serde(borrow)]
    o: Option<&'a RawValue>,
    #[serde(borrow)]
    h: Option<&'a RawValue>,
    #[serde(borrow)]
    l: Option<&'a RawValue>,
    #[serde(borrow)]
    v: Option<&'a RawValue>,
    #[serde(borrow)]
    n: Option<&'a RawValue>,
    #[serde(borrow)]
    vw: Option<&'a RawValue>,
}
impl RawRow<'_> {
    fn get(&self, key: &str) -> Option<&RawValue> {
        match key {
            "t" => self.t,
            "i" => self.i,
            "x" => self.x,
            "p" => self.p,
            "s" => self.s,
            "c" => self.c,
            "z" => self.z,
            "u" => self.u,
            "bx" => self.bx,
            "bp" => self.bp,
            "bs" => self.bs,
            "ax" => self.ax,
            "ap" => self.ap,
            "as" => self.ask_size_wire,
            "tks" => self.tks,
            "o" => self.o,
            "h" => self.h,
            "l" => self.l,
            "v" => self.v,
            "n" => self.n,
            "vw" => self.vw,
            _ => None,
        }
    }
    pub(crate) fn nanos(&self) -> Option<i64> {
        *self.parsed_nanos.get_or_init(|| {
            self.t
                .and_then(|value| wire_text(value.get()))
                .and_then(|value| lake_contracts::parse_iso_nanos(&value))
        })
    }
}

fn wire_text(raw: &str) -> Option<std::borrow::Cow<'_, str>> {
    serde_json::from_str::<&str>(raw)
        .ok()
        .map(std::borrow::Cow::Borrowed)
        .or_else(|| {
            serde_json::from_str::<String>(raw)
                .ok()
                .map(std::borrow::Cow::Owned)
        })
}

pub(crate) fn page_rows<'a>(
    payload: &'a [u8],
    data_type: DataType,
    symbol: &str,
) -> Result<(Vec<RawRow<'a>>, Option<String>), ProviderError> {
    let fields: BTreeMap<&str, &RawValue> = serde_json::from_slice(payload).map_err(json_error)?;
    let raw = fields.get(data_type.value()).ok_or_else(|| {
        protocol(format!(
            "Alpaca response is missing '{}'",
            data_type.value()
        ))
    })?;
    let raw = if raw.get().starts_with('{') {
        let symbols: BTreeMap<&str, &RawValue> =
            serde_json::from_str(raw.get()).map_err(json_error)?;
        symbols.get(symbol).copied()
    } else {
        Some(*raw)
    };
    let rows = match raw {
        None => Vec::new(),
        Some(v) if v.get() == "null" => Vec::new(),
        Some(v) => bounded_rows(v.get())?,
    };
    let cursor = fields
        .get("next_page_token")
        .map(|v| serde_json::from_str::<Option<String>>(v.get()).map_err(json_error))
        .transpose()?
        .flatten();
    Ok((rows, cursor))
}
/// Enforce the requested 10,000-row page contract before allocating a whole malformed page.
fn bounded_rows<'a>(raw: &'a str) -> Result<Vec<RawRow<'a>>, ProviderError> {
    struct Page;
    impl<'de> serde::de::Visitor<'de> for Page {
        type Value = Vec<RawRow<'de>>;
        fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("at most 10000 provider rows")
        }
        fn visit_seq<A: serde::de::SeqAccess<'de>>(
            self,
            mut sequence: A,
        ) -> Result<Self::Value, A::Error> {
            let mut rows = Vec::new();
            while let Some(row) = sequence.next_element()? {
                if rows.len() == 10000 {
                    return Err(serde::de::Error::custom(
                        "provider page exceeds requested row limit",
                    ));
                }
                rows.push(row);
            }
            Ok(rows)
        }
    }
    serde::de::Deserializer::deserialize_seq(&mut serde_json::Deserializer::from_str(raw), Page)
        .map_err(json_error)
}

fn json_error(error: serde_json::Error) -> ProviderError {
    protocol(format!("invalid Alpaca response: {error}"))
}

/// Parse one equity trade row.
pub(crate) fn parse_trade(
    row: &RawRow<'_>,
    symbol: &str,
) -> Result<NormalizedTrade, ProviderError> {
    let update = optional_text(row, "u")?;
    Ok(NormalizedTrade {
        timestamp: timestamp(row)?,
        symbol: symbol.into(),
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
pub(crate) fn parse_quote(
    row: &RawRow<'_>,
    symbol: &str,
) -> Result<NormalizedQuote, ProviderError> {
    Ok(NormalizedQuote {
        timestamp: timestamp(row)?,
        symbol: symbol.into(),
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
    row: &RawRow<'_>,
    symbol: &str,
) -> Result<NormalizedCryptoTrade, ProviderError> {
    Ok(NormalizedCryptoTrade {
        timestamp: timestamp(row)?,
        symbol: symbol.into(),
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
    row: &RawRow<'_>,
    symbol: &str,
) -> Result<NormalizedCryptoQuote, ProviderError> {
    Ok(NormalizedCryptoQuote {
        timestamp: timestamp(row)?,
        symbol: symbol.into(),
        bid_exchange: optional_text(row, "bx")?,
        bid_price: decimal(row, "bp")?,
        bid_size: decimal(row, "bs")?,
        ask_exchange: optional_text(row, "ax")?,
        ask_price: decimal(row, "ap")?,
        ask_size: decimal(row, "as")?,
    })
}

/// Parse one equity or crypto bar row.
///
/// The provider row carries `t` (bucket start), `o`, `h`, `l`, `c`, and `v`
/// (volume); `n` and `vw` are ignored because the pinned lake schema has no
/// trade-count or vwap column. Every numeric field is read as an exact decimal.
pub(crate) fn parse_bar(row: &RawRow<'_>, symbol: &str) -> Result<NormalizedBar, ProviderError> {
    Ok(NormalizedBar {
        timestamp: timestamp(row)?,
        symbol: symbol.into(),
        open: decimal(row, "o")?,
        high: decimal(row, "h")?,
        low: decimal(row, "l")?,
        close: decimal(row, "c")?,
        volume: decimal(row, "v")?,
    })
}

type OptionPageRows<'a> = (Vec<(&'a ContractRecord, RawRow<'a>)>, Option<String>);

/// Extract the rows and the next cursor from one option page.
///
/// Option pages are always keyed by symbol (`{"trades": {"SPY…": [...]}}`),
/// every key must be one of the requested contracts, and the rows are returned
/// in contract insertion order and then provider row order, mirroring
/// `_option_page_rows`.
pub(crate) fn option_page_rows<'a>(
    payload: &'a [u8],
    data_type: DataType,
    contracts: &'a [(String, ContractRecord)],
) -> Result<OptionPageRows<'a>, ProviderError> {
    let fields: BTreeMap<&str, &RawValue> = serde_json::from_slice(payload).map_err(json_error)?;
    let raw = fields
        .get(data_type.value())
        .ok_or_else(|| protocol("option response is missing data"))?;
    let mut symbols: BTreeMap<String, &RawValue> =
        serde_json::from_str(raw.get()).map_err(json_error)?;
    for name in symbols.keys() {
        if !contracts
            .iter()
            .any(|(s, _)| s == &name.trim().to_uppercase())
        {
            return Err(protocol(format!(
                "Alpaca option response contains unselected symbol '{name}'"
            )));
        }
    }
    let mut rows = Vec::new();
    for (symbol, contract) in contracts {
        let names: Vec<_> = symbols
            .keys()
            .filter(|name| name.trim().to_uppercase() == *symbol)
            .cloned()
            .collect();
        for name in names {
            let data = symbols.remove(&name).expect("selected symbol");
            let page = bounded_rows(data.get())?;
            if rows.len() + page.len() > 10000 {
                return Err(protocol("option page exceeds requested row limit"));
            }
            rows.extend(page.into_iter().map(|row| (contract, row)));
        }
    }
    let cursor = fields
        .get("next_page_token")
        .map(|v| serde_json::from_str::<Option<String>>(v.get()).map_err(json_error))
        .transpose()?
        .flatten();
    Ok((rows, cursor))
}

/// Parse one option trade row against the requested contract.
pub(crate) fn parse_option_trade(
    row: &RawRow<'_>,
    contract: &ContractRecord,
) -> Result<NormalizedOptionTrade, ProviderError> {
    Ok(NormalizedOptionTrade {
        timestamp: timestamp(row)?,
        symbol: contract.symbol.as_str().into(),
        underlying: contract.underlying.as_str().into(),
        expiration_date: contract.expiration_date,
        strike_price: contract.strike_price,
        option_type: contract.option_type,
        exchange: text(row, "x")?,
        price: decimal(row, "p")?,
        size: u32::try_from(integer(row, "s", 0, Some(i128::from(u32::MAX)))?)
            .expect("bounded by the maximum"),
        condition: text(row, "c")?,
    })
}

/// Parse one option quote row against the requested contract.
pub(crate) fn parse_option_quote(
    row: &RawRow<'_>,
    contract: &ContractRecord,
) -> Result<NormalizedOptionQuote, ProviderError> {
    Ok(NormalizedOptionQuote {
        timestamp: timestamp(row)?,
        symbol: contract.symbol.as_str().into(),
        underlying: contract.underlying.as_str().into(),
        expiration_date: contract.expiration_date,
        strike_price: contract.strike_price,
        option_type: contract.option_type,
        bid_exchange: text(row, "bx")?,
        bid_price: decimal(row, "bp")?,
        bid_size: u32::try_from(integer(row, "bs", 0, Some(i128::from(u32::MAX)))?)
            .expect("bounded by the maximum"),
        ask_exchange: text(row, "ax")?,
        ask_price: decimal(row, "ap")?,
        ask_size: u32::try_from(integer(row, "as", 0, Some(i128::from(u32::MAX)))?)
            .expect("bounded by the maximum"),
        condition: text(row, "c")?,
    })
}

fn required<'a>(row: &'a RawRow<'_>, key: &str) -> Result<&'a RawValue, ProviderError> {
    row.get(key)
        .ok_or_else(|| protocol(format!("Alpaca row is missing '{key}'")))
}
fn text(row: &RawRow<'_>, key: &str) -> Result<SmolStr, ProviderError> {
    let value: SmolStr = serde_json::from_str(required(row, key)?.get())
        .map_err(|_| protocol(format!("Alpaca field '{key}' must be a non-empty string")))?;
    if value.is_empty() {
        return Err(protocol(format!(
            "Alpaca field '{key}' must be a non-empty string"
        )));
    }
    Ok(value)
}
fn optional_text(row: &RawRow<'_>, key: &str) -> Result<Option<SmolStr>, ProviderError> {
    match row.get(key) {
        None => Ok(None),
        Some(v) if v.get() == "null" => Ok(None),
        _ => text(row, key).map(Some),
    }
}
fn integer(
    row: &RawRow<'_>,
    key: &str,
    minimum: i128,
    maximum: Option<i128>,
) -> Result<i128, ProviderError> {
    let value = required(row, key)?
        .get()
        .parse::<i128>()
        .map_err(|_| protocol(format!("Alpaca field '{key}' must be an integer")))?;
    if value < minimum || maximum.is_some_and(|max| value > max) {
        return Err(protocol(format!("Alpaca field '{key}' is out of range")));
    }
    Ok(value)
}
fn optional_integer(
    row: &RawRow<'_>,
    key: &str,
    minimum: i128,
    maximum: Option<i128>,
) -> Result<Option<i128>, ProviderError> {
    match row.get(key) {
        None => Ok(None),
        Some(v) if v.get() == "null" => Ok(None),
        _ => integer(row, key, minimum, maximum).map(Some),
    }
}
fn decimal(row: &RawRow<'_>, key: &str) -> Result<Decimal18, ProviderError> {
    let raw = required(row, key)?.get();
    let owned;
    let text = if raw.starts_with('"') {
        owned = serde_json::from_str::<String>(raw).map_err(|_| numeric_error(key))?;
        owned.as_str()
    } else {
        raw
    };
    if is_non_finite(text) {
        return Err(protocol(format!("Alpaca field '{key}' must be finite")));
    }
    Decimal18::parse(text.trim()).map_err(|_| numeric_error(key))
}
fn numeric_error(key: &str) -> ProviderError {
    protocol(format!("Alpaca field '{key}' must be numeric"))
}

fn is_non_finite(text: &str) -> bool {
    let body = text
        .strip_prefix('+')
        .or_else(|| text.strip_prefix('-'))
        .unwrap_or(text);
    body.eq_ignore_ascii_case("nan")
        || body.eq_ignore_ascii_case("inf")
        || body.eq_ignore_ascii_case("infinity")
        || (body
            .get(..3)
            .is_some_and(|prefix| prefix.eq_ignore_ascii_case("nan"))
            && body[3..].bytes().all(|byte| byte.is_ascii_digit()))
}

fn timestamp(row: &RawRow<'_>) -> Result<DateTime, ProviderError> {
    if let Some(nanos) = row.nanos() {
        return Ok(DateTime::from_micros(nanos.div_euclid(1000)));
    }
    let value = text(row, "t")?;
    if !has_timezone(&value) {
        return Err(protocol("Alpaca timestamp must include a timezone"));
    }
    Err(protocol("Alpaca timestamp must be ISO-8601"))
}

fn has_timezone(value: &str) -> bool {
    value.ends_with('Z')
        || value.rfind('+').is_some_and(|index| index > 10)
        || value.rfind('-').is_some_and(|index| index > 10)
}

fn conditions(row: &RawRow<'_>) -> Result<Vec<SmolStr>, ProviderError> {
    serde_json::from_str(required(row, "c")?.get())
        .map_err(|_| protocol("Alpaca conditions must be an array of strings"))
}

fn protocol(message: impl Into<String>) -> ProviderError {
    ProviderError::Protocol(message.into())
}
