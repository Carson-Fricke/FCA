//! SEC `company_tickers.json` ticker-to-CIK mapping.
//!
//! Decision D006 joins the Nasdaq symbol directory with the SEC ticker map so
//! the reference facts route can resolve fundamentals by symbol. The mapping is
//! baked into each daily classification snapshot's additive `cik` column, so
//! the snapshot's knowledge date is also the date of the mapping it carries
//! and later SEC revisions never leak into earlier as-of views.
//!
//! SIC codes are deliberately not populated here. The only EDGAR surface that
//! carries them is the per-CIK submissions document
//! (`data.sec.gov/submissions/CIK##########.json`); filling `sic` and
//! `sic_industry` would add one request per listed security (thousands), so the
//! columns stay absent until a bulk source is adopted. The schema and readers
//! already tolerate the columns, which keeps that change purely additive.
//!
//! The endpoint is pinned rather than configured: it is a stable SEC listing
//! file, and the committed settings have no field for it.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::error::ReferenceError;

/// The SEC listing map that assigns every ticker to its CIK.
pub const COMPANY_TICKERS_URL: &str = "https://www.sec.gov/files/company_tickers.json";

/// A parsed ticker-to-CIK mapping, keyed by canonical uppercase ticker.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CompanyTickers {
    by_ticker: BTreeMap<String, u64>,
}

impl CompanyTickers {
    /// Parse the SEC document shape: an object of `{index: {cik_str, ticker}}`.
    ///
    /// Every entry must carry a positive integer `cik_str` and a non-empty
    /// `ticker`; the `title` and any other fields are ignored. Duplicate
    /// tickers resolve to the smallest CIK so the map is deterministic
    /// regardless of the object key order.
    pub fn from_document(document: &Value) -> Result<Self, ReferenceError> {
        let Some(entries) = document.as_object() else {
            return Err(ReferenceError::tickers(
                "SEC company tickers payload root must be an object",
            ));
        };
        let mut pairs: Vec<(String, u64)> = Vec::with_capacity(entries.len());
        for (index, entry) in entries {
            let Some(entry) = entry.as_object() else {
                return Err(ReferenceError::tickers(format!(
                    "SEC company tickers entry {index:?} must be an object"
                )));
            };
            pairs.push((ticker_of(entry, index)?, cik_of(entry, index)?));
        }
        pairs.sort();
        let mut by_ticker: BTreeMap<String, u64> = BTreeMap::new();
        for (ticker, cik) in pairs {
            by_ticker.entry(ticker).or_insert(cik);
        }
        Ok(Self { by_ticker })
    }

    /// Parse a raw `company_tickers.json` payload.
    pub fn from_bytes(payload: &[u8]) -> Result<Self, ReferenceError> {
        let document: Value = serde_json::from_slice(payload).map_err(|error| {
            ReferenceError::tickers(format!(
                "SEC company tickers payload is not valid JSON: {error}"
            ))
        })?;
        Self::from_document(&document)
    }

    /// Resolve one symbol to its CIK, matching the trimmed uppercase ticker.
    ///
    /// The lookup is exact: Nasdaq dot-class spellings (`BRK.B`) and SEC
    /// dash-class spellings (`BRK-B`) are not aliased to each other.
    pub fn cik_for(&self, symbol: &str) -> Option<u64> {
        self.by_ticker.get(&canonical_ticker(symbol)).copied()
    }

    /// How many tickers the map carries.
    pub fn len(&self) -> usize {
        self.by_ticker.len()
    }

    /// Whether the map carries no tickers.
    pub fn is_empty(&self) -> bool {
        self.by_ticker.is_empty()
    }
}

fn canonical_ticker(value: &str) -> String {
    value.trim().to_ascii_uppercase()
}

fn ticker_of(
    entry: &serde_json::Map<String, Value>,
    index: &str,
) -> Result<String, ReferenceError> {
    let value = entry.get("ticker").ok_or_else(|| {
        ReferenceError::tickers(format!(
            "SEC company tickers entry {index:?} is missing a ticker"
        ))
    })?;
    let Some(text) = value.as_str() else {
        return Err(ReferenceError::tickers(format!(
            "SEC company tickers entry {index:?} is missing a ticker"
        )));
    };
    let canonical = canonical_ticker(text);
    if canonical.is_empty() {
        return Err(ReferenceError::tickers(format!(
            "SEC company tickers entry {index:?} is missing a ticker"
        )));
    }
    Ok(canonical)
}

fn cik_of(entry: &serde_json::Map<String, Value>, index: &str) -> Result<u64, ReferenceError> {
    let value = entry.get("cik_str").ok_or_else(|| {
        ReferenceError::tickers(format!(
            "SEC company tickers entry {index:?} is missing a positive integer cik_str"
        ))
    })?;
    if value.is_boolean() {
        return Err(ReferenceError::tickers(format!(
            "SEC company tickers entry {index:?} is missing a positive integer cik_str"
        )));
    }
    let Some(integer) = value.as_u64() else {
        return Err(ReferenceError::tickers(format!(
            "SEC company tickers entry {index:?} is missing a positive integer cik_str"
        )));
    };
    if integer == 0 {
        return Err(ReferenceError::tickers(format!(
            "SEC company tickers entry {index:?} is missing a positive integer cik_str"
        )));
    }
    Ok(integer)
}

#[cfg(test)]
#[path = "../tests/unit/company_tickers.rs"]
mod tests;
