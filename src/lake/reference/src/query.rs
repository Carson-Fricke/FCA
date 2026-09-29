//! Point-in-time queries over local reference dataset partitions.
//!
//! Ports the read half of `lake.reference.query`, retained as the parity
//! oracle for the partitions this crate writes, and extends it with a
//! blob-backed path so the published reference routes read the same partitions
//! through the storage read seam without re-implementing any point-in-time
//! rule. As-of semantics are the point-in-time contract: classification
//! returns the latest snapshot whose knowledge date is on or before `as_of`;
//! fundamentals return only facts whose `filed` date is on or before `as_of`,
//! so a restatement filed later never leaks into an earlier view. Partition
//! pruning by knowledge date is combined with the row-level `filed <= as_of`
//! predicate that defines the point-in-time contract.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use lake_contracts::Date;
use lake_storage::ReadableBlobStore;

use crate::error::ReferenceError;
use crate::schemas::{
    read_classification_rows_with_extras, read_fundamentals_rows, ClassificationExtras,
    ClassificationRow, FactRow,
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
    Ok(classification_snapshot_with_extras(root, as_of)?
        .into_iter()
        .map(|(row, _extras)| row)
        .collect())
}

/// [`classification_snapshot`] plus the optional per-row reference extras.
pub fn classification_snapshot_with_extras(
    root: &Path,
    as_of: Date,
) -> Result<Vec<(ClassificationRow, ClassificationExtras)>, ReferenceError> {
    let partitions = pruned_partitions(root, "classification", as_of);
    let Some(latest) = partitions
        .iter()
        .filter_map(|(relative, _)| reference_object_date(relative, "classification"))
        .max()
    else {
        return Ok(Vec::new());
    };
    let mut rows = Vec::new();
    for (relative, path) in partitions
        .iter()
        .filter(|(relative, _)| reference_object_date(relative, "classification") == Some(latest))
    {
        let payload = std::fs::read(path)?;
        rows.extend(read_classification_rows_with_extras(
            &payload,
            relative,
            "reference",
        )?);
    }
    sort_classification_rows(&mut rows);
    Ok(rows)
}

/// Return the rows of the newest snapshot read through the blob store.
pub fn classification_snapshot_from_store(
    store: &dyn ReadableBlobStore,
    as_of: Date,
) -> Result<Vec<ClassificationRow>, ReferenceError> {
    Ok(
        classification_snapshot_with_extras_from_store(store, as_of)?
            .into_iter()
            .map(|(row, _extras)| row)
            .collect(),
    )
}

/// [`classification_snapshot_from_store`] plus the optional per-row extras.
pub fn classification_snapshot_with_extras_from_store(
    store: &dyn ReadableBlobStore,
    as_of: Date,
) -> Result<Vec<(ClassificationRow, ClassificationExtras)>, ReferenceError> {
    let objects = pruned_store_objects(store, "classification", as_of)?;
    let Some(latest) = objects.last().map(|(date, _)| *date) else {
        return Ok(Vec::new());
    };
    let mut rows = Vec::new();
    for (_date, relative) in objects.iter().filter(|(date, _)| *date == latest) {
        let payload = store.read(relative)?;
        rows.extend(read_classification_rows_with_extras(
            &payload,
            relative,
            "reference",
        )?);
    }
    sort_classification_rows(&mut rows);
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

/// [`fundamentals_facts`] over the injected blob store.
pub fn fundamentals_facts_from_store(
    store: &dyn ReadableBlobStore,
    as_of: Date,
    filter: FactFilter<'_>,
) -> Result<Vec<FactRow>, ReferenceError> {
    let mut rows: Vec<FactRow> = Vec::new();
    for (_date, relative) in pruned_store_objects(store, "fundamentals", as_of)? {
        let payload = store.read(&relative)?;
        for row in read_fundamentals_rows(&payload, &relative, "reference")? {
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
    Ok(latest_visible(fundamentals_facts(root, as_of, filter)?))
}

/// [`latest_fundamentals`] over the injected blob store.
pub fn latest_fundamentals_from_store(
    store: &dyn ReadableBlobStore,
    as_of: Date,
    filter: FactFilter<'_>,
) -> Result<Vec<FactRow>, ReferenceError> {
    Ok(latest_visible(fundamentals_facts_from_store(
        store, as_of, filter,
    )?))
}

/// Keep the greatest-recency row per XBRL identity.
fn latest_visible(rows: Vec<FactRow>) -> Vec<FactRow> {
    let mut latest: BTreeMap<(u64, String, String, String, Date), FactRow> = BTreeMap::new();
    for row in rows {
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
    winners
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

/// Sort snapshot rows the way the local oracle always has.
fn sort_classification_rows(rows: &mut [(ClassificationRow, ClassificationExtras)]) {
    rows.sort_by(|(left, _), (right, _)| {
        left.file_creation_time
            .cmp(&right.file_creation_time)
            .then_with(|| left.symbol.cmp(&right.symbol))
            .then_with(|| left.security_name.cmp(&right.security_name))
            .then_with(|| left.listing_exchange.cmp(&right.listing_exchange))
            .then_with(|| left.market_category.cmp(&right.market_category))
    });
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

/// Return the relative paths of every reference object known on or before
/// `as_of`, sorted by knowledge date then relative path.
fn pruned_store_objects(
    store: &dyn ReadableBlobStore,
    data_type: &str,
    as_of: Date,
) -> Result<Vec<(Date, String)>, ReferenceError> {
    let mut pruned: Vec<(Date, String)> = Vec::new();
    for relative in store.list_prefix("")? {
        let Some(knowledge_date) = reference_object_date(&relative, data_type) else {
            continue;
        };
        if knowledge_date <= as_of {
            pruned.push((knowledge_date, relative));
        }
    }
    pruned.sort_by(|left, right| left.0.cmp(&right.0).then_with(|| left.1.cmp(&right.1)));
    Ok(pruned)
}

/// Parse one reference object's knowledge date from its canonical Hive path.
fn reference_object_date(relative_path: &str, data_type: &str) -> Option<Date> {
    let mut parts = relative_path.split('/');
    let knowledge_date = parts.next()?.strip_prefix("date=").and_then(Date::parse)?;
    if parts.next()? != "asset_class=reference" {
        return None;
    }
    if parts.next()? != format!("data_type={data_type}") {
        return None;
    }
    let object_name = parts.next()?;
    if parts.next().is_some() || !object_name.ends_with(".parquet") {
        return None;
    }
    Some(knowledge_date)
}
