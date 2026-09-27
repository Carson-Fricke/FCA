//! Point-in-time queries over local reference dataset partitions.
//!
//! Ports the read half of `lake.reference.query`, retained as the parity
//! oracle for the partitions this crate writes. As-of semantics are the
//! point-in-time contract: classification returns the latest snapshot whose
//! knowledge date is on or before `as_of`; fundamentals return only facts
//! whose `filed` date is on or before `as_of`, so a restatement filed later
//! never leaks into an earlier view. Partition pruning by knowledge date is
//! combined with the row-level `filed <= as_of` predicate that defines the
//! point-in-time contract.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use lake_contracts::Date;

use crate::error::ReferenceError;
use crate::schemas::{
    read_classification_rows, read_fundamentals_rows, ClassificationRow, FactRow,
};

/// Optional fact narrowing filters.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct FactFilter<'a> {
    /// Keep only this SEC CIK.
    pub cik: Option<u64>,
    /// Keep only this taxonomy.
    pub taxonomy: Option<&'a str>,
    /// Keep only this XBRL tag.
    pub tag: Option<&'a str>,
    /// Keep only this unit.
    pub unit: Option<&'a str>,
}

impl<'a> FactFilter<'a> {
    /// An unfiltered view.
    pub fn none() -> Self {
        Self::default()
    }
}

/// Return the rows of the newest snapshot known on or before `as_of`.
///
/// Snapshots are self-contained daily objects, so partition pruning on the
/// knowledge date is the whole selection: a later snapshot contributes
/// nothing to an earlier as-of and no earlier row survives its replacement.
pub fn classification_snapshot(
    root: &Path,
    as_of: Date,
) -> Result<Vec<ClassificationRow>, ReferenceError> {
    let Some(latest) = pruned_partitions(root, "classification", as_of).pop() else {
        return Ok(Vec::new());
    };
    let payload = std::fs::read(&latest.1)?;
    let mut rows = read_classification_rows(&payload, &latest.0, "reference")?;
    rows.sort_by(|left, right| {
        left.file_creation_time
            .cmp(&right.file_creation_time)
            .then_with(|| left.symbol.cmp(&right.symbol))
            .then_with(|| left.security_name.cmp(&right.security_name))
            .then_with(|| left.listing_exchange.cmp(&right.listing_exchange))
            .then_with(|| left.market_category.cmp(&right.market_category))
    });
    Ok(rows)
}

/// Return every fact row visible at `as_of`.
///
/// Only partitions whose knowledge date (the `filed` date) is on or before
/// `as_of` are read, and each surviving row is re-checked against the same
/// `filed <= as_of` predicate so the contract holds even for objects that mix
/// knowledge dates. The filters narrow the result when supplied.
pub fn fundamentals_facts(
    root: &Path,
    as_of: Date,
    filter: FactFilter<'_>,
) -> Result<Vec<FactRow>, ReferenceError> {
    let mut rows: Vec<FactRow> = Vec::new();
    for (path_string, path) in pruned_partitions(root, "fundamentals", as_of) {
        let payload = std::fs::read(&path)?;
        for row in read_fundamentals_rows(&payload, &path_string, "reference")? {
            if visible(&row, as_of, &filter) {
                rows.push(row);
            }
        }
    }
    rows.sort();
    Ok(rows)
}

/// Return one fact per XBRL identity, as known at `as_of`.
///
/// The identity is `(cik, taxonomy, tag, unit, period_end)`; among the
/// visible rows the one with the greatest `(filed, accn)` wins, so a
/// restatement replaces the original only from its own filing date onward and
/// earlier as-of instants resolve to the originally filed value.
pub fn latest_fundamentals(
    root: &Path,
    as_of: Date,
    filter: FactFilter<'_>,
) -> Result<Vec<FactRow>, ReferenceError> {
    let mut latest: BTreeMap<(u64, String, String, String, Date), FactRow> = BTreeMap::new();
    for row in fundamentals_facts(root, as_of, filter)? {
        let identity = (
            row.cik,
            row.taxonomy.clone(),
            row.tag.clone(),
            row.unit.clone(),
            row.period_end,
        );
        let replace = match latest.get(&identity) {
            None => true,
            Some(current) => row.recency() > current.recency(),
        };
        if replace {
            latest.insert(identity, row);
        }
    }
    let mut winners: Vec<FactRow> = latest.into_values().collect();
    winners.sort();
    Ok(winners)
}

fn visible(row: &FactRow, as_of: Date, filter: &FactFilter<'_>) -> bool {
    if row.filed > as_of {
        return false;
    }
    if filter.cik.is_some_and(|cik| row.cik != cik) {
        return false;
    }
    if filter
        .taxonomy
        .is_some_and(|taxonomy| row.taxonomy != taxonomy)
    {
        return false;
    }
    if filter.tag.is_some_and(|tag| row.tag != tag) {
        return false;
    }
    !filter.unit.is_some_and(|unit| row.unit != unit)
}

/// Return the `(relative path, absolute path)` pairs of every Parquet object
/// whose `date=` knowledge date is on or before `as_of`, sorted by knowledge
/// date then relative path.
fn pruned_partitions(root: &Path, data_type: &str, as_of: Date) -> Vec<(String, PathBuf)> {
    let mut pruned: Vec<(Date, String, PathBuf)> = Vec::new();
    let Ok(entries) = std::fs::read_dir(root) else {
        return Vec::new();
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        let Some(knowledge_date) = name.strip_prefix("date=").and_then(Date::parse) else {
            continue;
        };
        if knowledge_date > as_of {
            continue;
        }
        let directory = entry
            .path()
            .join("asset_class=reference")
            .join(format!("data_type={data_type}"));
        let Ok(objects) = std::fs::read_dir(&directory) else {
            continue;
        };
        for object in objects.flatten() {
            let object_name = object.file_name();
            let Some(object_name) = object_name.to_str() else {
                continue;
            };
            if !object_name.ends_with(".parquet") {
                continue;
            }
            let relative = format!(
                "date={knowledge_date}/asset_class=reference/data_type={data_type}/{object_name}"
            );
            pruned.push((knowledge_date, relative, object.path()));
        }
    }
    pruned.sort_by(|left, right| left.0.cmp(&right.0).then_with(|| left.1.cmp(&right.1)));
    pruned
        .into_iter()
        .map(|(_date, relative, path)| (relative, path))
        .collect()
}
