//! Immutable, canonical market-symbol value objects.
//!
//! Mirrors `harness.core.symbols`: construction is the single normalization
//! point, option symbols use the compact OCC/OSI representation, and the
//! provider-specific spelling never reaches deterministic state.

use core::fmt;

use num::Decimal18;

use crate::error::LiveError;
use crate::error::LiveResult;

/// A canonical, immutable market symbol.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Symbol(String);

impl Symbol {
    /// Validate and canonicalize a market symbol (trimmed and upper-cased).
    pub fn new(value: &str) -> LiveResult<Self> {
        let canonical = value.trim().to_ascii_uppercase();
        if canonical.is_empty() {
            return Err(LiveError::Validation("symbol must not be empty".into()));
        }
        if !is_market_symbol(&canonical) {
            return Err(LiveError::Validation(format!(
                "invalid market symbol: {value:?}"
            )));
        }
        Ok(Self(canonical))
    }

    /// The canonical string value.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for Symbol {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// A canonical OCC/OSI option symbol.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct OsiSymbol {
    raw: String,
    root: String,
    expiration: String,
    right: char,
    strike: Decimal18,
}

impl OsiSymbol {
    /// Parse a compact or six-character-padded OSI symbol.
    pub fn new(value: &str) -> LiveResult<Self> {
        let mut candidate = value.trim().to_ascii_uppercase();
        if candidate.len() >= 21 && candidate.as_bytes()[..6].contains(&b' ') {
            let root = candidate[..6].trim_end().to_string();
            candidate = format!("{root}{}", &candidate[6..]);
        }
        let bytes = candidate.as_bytes();
        if bytes.len() < 16 {
            return Err(LiveError::Validation(format!(
                "invalid OSI symbol: {value:?}"
            )));
        }
        let (body, tail) = candidate.split_at(candidate.len() - 15);
        if tail.len() != 15 {
            return Err(LiveError::Validation(format!(
                "invalid OSI symbol: {value:?}"
            )));
        }
        if body.is_empty() || body.len() > 6 || !body.bytes().all(is_root_byte) {
            return Err(LiveError::Validation(format!(
                "invalid OSI symbol: {value:?}"
            )));
        }
        let exp = &tail[..6];
        let right = tail.as_bytes()[6] as char;
        let strike_text = &tail[7..];
        if !exp.bytes().all(|b| b.is_ascii_digit())
            || !(right == 'C' || right == 'P')
            || !strike_text.bytes().all(|b| b.is_ascii_digit())
        {
            return Err(LiveError::Validation(format!(
                "invalid OSI symbol: {value:?}"
            )));
        }
        let strike_int: i128 = strike_text
            .parse()
            .map_err(|_| LiveError::Validation(format!("invalid OSI symbol: {value:?}")))?;
        let strike = Decimal18::from_i128(strike_int)
            .and_then(|value| value.checked_div(Decimal18::from_i64(1_000)))
            .map_err(|_| LiveError::Validation(format!("invalid OSI symbol: {value:?}")))?;
        let root = body.to_string();
        let expiration = exp.to_string();
        Ok(Self {
            raw: candidate,
            root,
            expiration,
            right,
            strike,
        })
    }

    /// The canonical compact OSI string.
    pub fn as_str(&self) -> &str {
        &self.raw
    }

    /// The underlying root symbol.
    pub fn root(&self) -> &str {
        &self.root
    }

    /// Expiration as `YYMMDD`.
    pub fn expiration(&self) -> &str {
        &self.expiration
    }

    /// `C` or `P`.
    pub fn option_type(&self) -> char {
        self.right
    }

    /// The OSI strike as an exact decimal.
    pub fn strike(&self) -> Decimal18 {
        self.strike
    }

    /// The canonical [`Symbol`] that carries this OSI text.
    pub fn to_symbol(&self) -> Symbol {
        Symbol(self.raw.clone())
    }
}

/// Return a canonical [`Symbol`], accepting a compact OSI symbol unchanged.
pub fn canonical_symbol(value: &str) -> LiveResult<Symbol> {
    if let Ok(osi) = OsiSymbol::new(value) {
        return Ok(osi.to_symbol());
    }
    Symbol::new(value)
}

/// Whether `value` is a valid compact or padded OSI symbol.
pub fn is_osi_symbol(value: &str) -> bool {
    OsiSymbol::new(value).is_ok()
}

fn is_root_byte(byte: u8) -> bool {
    byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'.'
}

fn is_market_symbol(value: &str) -> bool {
    let bytes = value.as_bytes();
    let Some((&first, rest)) = bytes.split_first() else {
        return false;
    };
    if !(first.is_ascii_uppercase() || first.is_ascii_digit()) {
        return false;
    }
    rest.iter().all(|&byte| {
        byte.is_ascii_uppercase()
            || byte.is_ascii_digit()
            || matches!(byte, b'.' | b'/' | b':' | b'_' | b'-')
    })
}
