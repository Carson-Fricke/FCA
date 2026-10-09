//! Dependency-light market declarations and persisted configuration hashing.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write;

pub const CONFIGURATION_HASH_DOMAIN: &[u8] = b"fca:harness-configuration:v1\0";

/// Both encodings shipped under v1. Callers must retain their persisted encoding.
#[derive(Clone, Copy, Debug)]
pub enum HashEncoding {
    Utf8,
    HistoricalAscii,
}

pub fn canonical_json(value: &Value, encoding: HashEncoding) -> String {
    fn write(value: &Value, out: &mut String) {
        match value {
            Value::Object(map) => {
                out.push('{');
                let sorted: BTreeMap<_, _> = map.iter().collect();
                for (index, (key, value)) in sorted.into_iter().enumerate() {
                    if index != 0 {
                        out.push(',');
                    }
                    out.push_str(&serde_json::to_string(key).expect("JSON string"));
                    out.push(':');
                    write(value, out);
                }
                out.push('}');
            }
            Value::Array(items) => {
                out.push('[');
                for (index, value) in items.iter().enumerate() {
                    if index != 0 {
                        out.push(',');
                    }
                    write(value, out);
                }
                out.push(']');
            }
            _ => out.push_str(&value.to_string()),
        }
    }
    let mut output = String::new();
    write(value, &mut output);
    if matches!(encoding, HashEncoding::Utf8) {
        return output;
    }
    let mut ascii = String::new();
    for character in output.chars() {
        if character.is_ascii() {
            ascii.push(character);
        } else {
            let mut buffer = [0; 2];
            for unit in character.encode_utf16(&mut buffer) {
                write!(ascii, "\\u{unit:04x}").expect("string write");
            }
        }
    }
    ascii
}

pub fn configuration_hash(value: &Value, encoding: HashEncoding) -> String {
    let mut hash = Sha256::new();
    hash.update(CONFIGURATION_HASH_DOMAIN);
    hash.update(canonical_json(value, encoding).as_bytes());
    hex::encode(hash.finalize())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DataType {
    Bars,
    Quote,
    Trade,
}

/// Validated domain interval, serialized as its canonical string token.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct BarInterval(String);

impl BarInterval {
    pub fn parse(value: &str) -> Result<Self, String> {
        validate_bar_interval(value)?;
        Ok(Self(value.to_string()))
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl<'de> Deserialize<'de> for BarInterval {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        Self::parse(&String::deserialize(deserializer)?).map_err(serde::de::Error::custom)
    }
}

impl DataType {
    pub fn parse(value: &str) -> Result<Self, String> {
        match value {
            "bars" => Ok(Self::Bars),
            "quote" => Ok(Self::Quote),
            "trade" => Ok(Self::Trade),
            _ => Err(format!("unknown data type {value:?}")),
        }
    }
}

/// Positive count and canonical unit; execution support is a separate policy.
pub fn validate_bar_interval(value: &str) -> Result<(), String> {
    let digits = value.bytes().take_while(u8::is_ascii_digit).count();
    if digits == 0 || value.starts_with('0') || !matches!(&value[digits..], "s" | "min" | "h" | "d")
    {
        return Err(format!("invalid bar interval {value:?}"));
    }
    Ok(())
}

/// Completed replay cadences supported by canonical minute-source aggregation.
/// `None` denotes one exchange session day; finite widths never cross sessions.
pub fn completed_bar_interval_ns(interval: &str) -> Result<Option<i64>, String> {
    validate_bar_interval(interval)?;
    if interval == "1d" {
        return Ok(None);
    }
    let (number, scale) = if let Some(n) = interval.strip_suffix("min") {
        (n, 60_000_000_000_i64)
    } else if let Some(n) = interval.strip_suffix('h') {
        (n, 3_600_000_000_000_i64)
    } else {
        return Err("replay bars require minute/hour intervals or 1d".into());
    };
    number
        .parse::<i64>()
        .ok()
        .and_then(|n| n.checked_mul(scale))
        .filter(|n| *n > 0)
        .map(Some)
        .ok_or_else(|| "bar interval overflow".into())
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "data_type", deny_unknown_fields)]
pub enum Subscription {
    #[serde(rename = "bars")]
    Bars {
        symbols: Vec<String>,
        interval: String,
    },
    #[serde(rename = "quote")]
    Quote { symbols: Vec<String> },
    #[serde(rename = "trade")]
    Trade { symbols: Vec<String> },
}

impl Subscription {
    pub fn symbols(&self) -> &[String] {
        match self {
            Self::Bars { symbols, .. } | Self::Quote { symbols } | Self::Trade { symbols } => {
                symbols
            }
        }
    }
}

pub fn canonical_subscriptions(inputs: Vec<Subscription>) -> Result<Vec<Subscription>, String> {
    let mut groups: BTreeMap<(DataType, Option<String>), BTreeSet<String>> = BTreeMap::new();
    for input in inputs {
        let key = match &input {
            Subscription::Bars { interval, .. } => {
                let interval = BarInterval::parse(interval)?;
                (DataType::Bars, Some(interval.as_str().to_string()))
            }
            Subscription::Quote { .. } => (DataType::Quote, None),
            Subscription::Trade { .. } => (DataType::Trade, None),
        };
        if input.symbols().is_empty()
            || input
                .symbols()
                .iter()
                .any(|s| s.trim().is_empty() || s.trim() != s)
        {
            return Err("subscriptions require nonempty, unpadded symbols".into());
        }
        groups
            .entry(key)
            .or_default()
            .extend(input.symbols().iter().cloned());
    }
    Ok(groups
        .into_iter()
        .map(|((kind, interval), symbols)| {
            let symbols = symbols.into_iter().collect();
            match kind {
                DataType::Bars => Subscription::Bars {
                    symbols,
                    interval: interval.expect("bars interval"),
                },
                DataType::Quote => Subscription::Quote { symbols },
                DataType::Trade => Subscription::Trade { symbols },
            }
        })
        .collect())
}
