//! Canonical market-symbol validation.
//!
//! Mirrors `harness.core.symbols`: symbols are uppercased and stripped, must
//! be non-empty, and must match the market-symbol character rule. OSI option
//! symbols are validated against the compact OCC representation.

use crate::error::DataError;

/// Return the canonical uppercase symbol or a validation error.
pub fn canonical_symbol(value: &str) -> Result<String, DataError> {
    let canonical = value.trim().to_uppercase();
    if canonical.is_empty() {
        return Err(DataError::value("symbol must not be empty"));
    }
    let first = canonical.chars().next().expect("non-empty after check");
    if !first.is_ascii_uppercase() && !first.is_ascii_digit() {
        return Err(DataError::value(format!(
            "invalid market symbol: {value:?}"
        )));
    }
    if !canonical
        .chars()
        .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || "._/:-".contains(c))
    {
        return Err(DataError::value(format!(
            "invalid market symbol: {value:?}"
        )));
    }
    Ok(canonical)
}

/// Return the canonical compact OSI option symbol or a validation error.
pub fn canonical_osi_symbol(value: &str) -> Result<String, DataError> {
    let upper = value.trim().to_uppercase();
    if !upper.is_ascii() {
        return Err(DataError::value(format!("invalid OSI symbol: {value:?}")));
    }
    let mut candidate = upper;
    if candidate.len() >= 21 && candidate[..6].contains(' ') {
        let root: String = candidate[..6].trim_end().to_string();
        candidate = format!("{root}{}", &candidate[6..]);
    }
    if !is_osi(&candidate) {
        return Err(DataError::value(format!("invalid OSI symbol: {value:?}")));
    }
    Ok(candidate)
}

/// Return whether `value` is a compact OSI option symbol.
pub fn is_osi_symbol(value: &str) -> bool {
    let upper = value.trim().to_uppercase();
    upper.is_ascii() && is_osi(&upper)
}

/// Return the canonical `BASE/QUOTE` lake pair for a dashed crypto symbol.
///
/// Research models name crypto instruments with a dash (`BTC-USD`), while the
/// market lake stores them as pairs (`BTC/USD`). Equity tickers that carry a
/// dash (`BRK-B`) are indistinguishable from a crypto pair by shape alone; the
/// caller resolves the ambiguity by attempting the equity partition first and
/// falling back to this pair form. `None` means the symbol is not shaped like a
/// pair and should be treated as equity.
#[must_use]
pub fn crypto_pair(symbol: &str) -> Option<String> {
    let upper = symbol.trim().to_uppercase();
    if let Some((base, quote)) = upper.split_once('/') {
        return pair_segments(base, quote).then(|| format!("{base}/{quote}"));
    }
    let (base, quote) = upper.split_once('-')?;
    pair_segments(base, quote).then(|| format!("{base}/{quote}"))
}

fn pair_segments(base: &str, quote: &str) -> bool {
    (2..=6).contains(&base.len())
        && (2..=6).contains(&quote.len())
        && base.chars().all(|c| c.is_ascii_alphabetic())
        && quote.chars().all(|c| c.is_ascii_alphabetic())
}

fn is_osi(value: &str) -> bool {
    if !value.is_ascii() {
        return false;
    }
    let bytes = value.as_bytes();
    let Some(split) = bytes.len().checked_sub(15) else {
        return false;
    };
    if split == 0 || split > 6 {
        return false;
    }
    let root = &value[..split];
    if !root
        .chars()
        .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '.')
    {
        return false;
    }
    let tail = &value[split..];
    let tail = tail.as_bytes();
    if tail.len() != 15 {
        return false;
    }
    let digits_ok =
        tail[..6].iter().all(u8::is_ascii_digit) && tail[7..].iter().all(u8::is_ascii_digit);
    let right_ok = tail[6] == b'C' || tail[6] == b'P';
    digits_ok && right_ok
}

#[cfg(test)]
#[path = "../tests/unit/symbol.rs"]
mod tests;
