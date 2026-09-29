//! Date-first Hive object paths for point-in-time reference datasets.
//!
//! Ports `lake.reference.partitions`. The stable relative path contract
//! mirrors market data:
//!
//! ```text
//! date=YYYY-MM-DD/asset_class=reference/data_type=<type>/<name>.parquet
//! ```
//!
//! Classification snapshots are one directory-wide object per knowledge date.
//! Fundamentals are one object per SEC CIK per `filed` knowledge date, so
//! as-of queries prune whole partitions on `filed <= as_of`. Object names are
//! canonicalized to uppercase and reuse the market-data symbol filename
//! rules.

use lake_contracts::{symbol_filename, AssetClass, DataType, Date};

use crate::error::ReferenceError;

/// The object name of the daily classification snapshot.
pub const CLASSIFICATION_OBJECT_NAME: &str = "SNAPSHOT";

/// The zero-padded digit count of a SEC CIK symbol.
const CIK_DIGITS: usize = 10;

/// Return the zero-padded 10-digit catalog symbol for a SEC CIK.
///
/// Mirrors Python's `cik_symbol`: zero and non-positive CIKs, and CIKs wider
/// than ten digits, are rejected.
pub fn cik_symbol(cik: u64) -> Result<String, ReferenceError> {
    if cik == 0 {
        return Err(ReferenceError::invalid(format!("invalid SEC CIK: {cik}")));
    }
    if cik >= 10u64.pow(CIK_DIGITS as u32) {
        return Err(ReferenceError::invalid(format!(
            "SEC CIK exceeds {CIK_DIGITS} digits: {cik}"
        )));
    }
    Ok(format!("{cik:0CIK_DIGITS$}"))
}

/// Return the self-describing Parquet object name for a SEC CIK.
pub fn cik_object_name(cik: u64) -> Result<String, ReferenceError> {
    Ok(format!("CIK{}", cik_symbol(cik)?))
}

/// Parse a zero-padded 10-digit catalog symbol back into a SEC CIK.
pub fn parse_cik_symbol(value: &str) -> Result<u64, ReferenceError> {
    let canonical = value.trim();
    if canonical.len() != CIK_DIGITS || !canonical.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(ReferenceError::invalid(format!(
            "invalid reference CIK symbol: {value:?}"
        )));
    }
    canonical
        .parse::<u64>()
        .map_err(|_| ReferenceError::invalid(format!("invalid reference CIK symbol: {value:?}")))
}

/// Build a repository-independent, date-first Hive object path.
///
/// Reference data is classification and fundamentals only: bars and every
/// other market data type are rejected explicitly, never aliased.
pub fn reference_partition_path(
    knowledge_date: Date,
    data_type: DataType,
    name: &str,
) -> Result<String, ReferenceError> {
    if matches!(data_type, DataType::Bars)
        || !matches!(data_type, DataType::Classification | DataType::Fundamentals)
    {
        return Err(ReferenceError::invalid(format!(
            "data_type is not a reference dataset: {}",
            data_type.value()
        )));
    }
    let canonical_name = name.trim().to_uppercase();
    if canonical_name.is_empty() {
        return Err(ReferenceError::invalid(
            "reference object name must not be empty",
        ));
    }
    Ok(format!(
        "date={}/asset_class={}/data_type={}/{}",
        knowledge_date.isoformat(),
        AssetClass::Reference.value(),
        data_type.value(),
        symbol_filename(&canonical_name)?
    ))
}

/// Whether the decimal text names a non-finite value in Python's
/// `Decimal` grammar (`NaN`, `Infinity`, and their signed spellings).
pub(crate) fn is_non_finite_decimal(text: &str) -> bool {
    let body = text
        .trim()
        .trim_start_matches(['+', '-'])
        .to_ascii_lowercase();
    body == "nan" || body == "inf" || body == "infinity" || body == "snan"
}

#[cfg(test)]
#[path = "../tests/unit/partitions.rs"]
mod tests;
