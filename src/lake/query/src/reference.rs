//! Point-in-time reference facts for the published reference routes.
//!
//! The universe and facts routes reuse `lake-reference`'s point-in-time
//! selection (latest classification snapshot on or before the as-of date,
//! facts filed on or before it, greatest `(filed, accn, period_end)` per
//! field) and only shape the result for the route contract. Classification
//! contributes `security_type`, `exchange`, `security_name`,
//! `market_category`, `round_lot_size`, and the optional SIC fields; a
//! symbol-to-CIK mapping carried by the snapshot joins the fundamentals
//! fields and their per-field filing dates.

use std::collections::{BTreeMap, BTreeSet};

use lake_contracts::Date;
use lake_reference::{
    classification_snapshot_with_extras_from_store, latest_fundamentals_from_store,
    ClassificationExtras, ClassificationRow, FactFilter, FactRow,
};
use lake_storage::ReadableBlobStore;

use crate::error::QueryApplicationError;

/// The screener fields and the EDGAR facts that resolve them.
///
/// Each entry is `(field, taxonomy, candidate tags, unit)`. The latest visible
/// fact among the candidate tags wins, keyed on `(filed, accn, period_end)`,
/// exactly like the retired screener context.
const FUNDAMENTAL_FIELDS: &[(&str, &str, &[&str], &str)] = &[
    (
        "shares_outstanding",
        "dei",
        &["EntityCommonStockSharesOutstanding"],
        "shares",
    ),
    (
        "revenue",
        "us-gaap",
        &[
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        ],
        "USD",
    ),
    ("net_income", "us-gaap", &["NetIncomeLoss"], "USD"),
    ("eps", "us-gaap", &["EarningsPerShareDiluted"], "USD/shares"),
];

/// Canonical equity symbols with a listing in the snapshot known at `as_of`.
///
/// A missing snapshot is an empty universe, never an error. Symbols are
/// canonical uppercase, deduplicated, and sorted.
pub fn universe_symbols(
    store: &dyn ReadableBlobStore,
    as_of: Date,
) -> Result<Vec<String>, QueryApplicationError> {
    let rows = classification_snapshot_with_extras_from_store(store, as_of).map_err(internal)?;
    let mut symbols: BTreeSet<String> = BTreeSet::new();
    for (row, _extras) in rows {
        let symbol = canonical(&row.symbol);
        if !symbol.is_empty() {
            symbols.insert(symbol);
        }
    }
    Ok(symbols.into_iter().collect())
}

/// Point-in-time classification and fundamentals facts for `symbols`.
///
/// Every requested symbol appears in the result; unknown or absent symbols map
/// to an empty fact sheet. Missing fields are omitted, never null.
pub fn symbol_facts(
    store: &dyn ReadableBlobStore,
    as_of: Date,
    symbols: &[String],
) -> Result<BTreeMap<String, BTreeMap<String, String>>, QueryApplicationError> {
    let snapshot =
        classification_snapshot_with_extras_from_store(store, as_of).map_err(internal)?;
    let mut latest: BTreeMap<String, (ClassificationRow, ClassificationExtras)> = BTreeMap::new();
    for (row, extras) in snapshot {
        latest.insert(canonical(&row.symbol), (row, extras));
    }

    let mut fundamentals_by_cik: BTreeMap<u64, Vec<FactRow>> = BTreeMap::new();
    let mut result = BTreeMap::new();
    for requested in symbols {
        let symbol = canonical(requested);
        let mut facts = BTreeMap::new();
        if let Some((row, extras)) = latest.get(&symbol) {
            facts.insert(
                "security_type".to_string(),
                if row.etf { "etf" } else { "stock" }.to_string(),
            );
            facts.insert(
                "exchange".to_string(),
                row.listing_exchange.trim().to_uppercase(),
            );
            facts.insert(
                "security_name".to_string(),
                row.security_name.trim().to_string(),
            );
            facts.insert(
                "market_category".to_string(),
                row.market_category.trim().to_string(),
            );
            facts.insert(
                "round_lot_size".to_string(),
                row.round_lot_size.to_canonical_string(),
            );
            if let Some(sic) = extras.sic.as_deref() {
                facts.insert("sic_code".to_string(), sic.trim().to_string());
            }
            if let Some(industry) = extras.sic_industry.as_deref() {
                facts.insert("sic_industry".to_string(), industry.trim().to_string());
            }
            if let Some(cik) = extras.cik {
                let rows = match fundamentals_by_cik.entry(cik) {
                    std::collections::btree_map::Entry::Occupied(entry) => entry.into_mut(),
                    std::collections::btree_map::Entry::Vacant(entry) => {
                        let rows = latest_fundamentals_from_store(
                            store,
                            as_of,
                            FactFilter {
                                cik: Some(cik),
                                ..FactFilter::none()
                            },
                        )
                        .map_err(internal)?;
                        entry.insert(rows)
                    }
                };
                for (field, taxonomy, tags, unit) in FUNDAMENTAL_FIELDS {
                    if let Some((value, filed)) = resolve_field(rows, taxonomy, tags, unit) {
                        facts.insert((*field).to_string(), value);
                        facts.insert(format!("{field}_filed"), filed.isoformat());
                    }
                }
            }
        }
        result.insert(symbol, facts);
    }
    Ok(result)
}

/// Resolve one field's latest visible fact among its candidate tags.
fn resolve_field(
    rows: &[FactRow],
    taxonomy: &str,
    tags: &[&str],
    unit: &str,
) -> Option<(String, Date)> {
    let mut best: Option<&FactRow> = None;
    let mut best_key: Option<(Date, &str, Date)> = None;
    for row in rows {
        if row.taxonomy != taxonomy || row.unit != unit || !tags.contains(&row.tag.as_str()) {
            continue;
        }
        let key = (row.filed, row.accn.as_str(), row.period_end);
        if best_key.is_none_or(|current| key > current) {
            best = Some(row);
            best_key = Some(key);
        }
    }
    best.map(|row| (row.val.to_canonical_string(), row.filed))
}

/// Canonicalize one request or snapshot symbol.
fn canonical(symbol: &str) -> String {
    symbol.trim().to_uppercase()
}

/// Map a reference read failure onto the internal application error.
fn internal(error: impl std::fmt::Display) -> QueryApplicationError {
    QueryApplicationError::new("internal_error", error.to_string(), None)
}
