//! Calendar-aware coverage calculations and gap planning.
//!
//! Coverage is evaluated for the sessions produced by the lake contracts
//! calendars, not for every calendar date. Empty catalog outcomes are
//! terminal, while partial sessions remain actionable so a later write can
//! replace the same deterministic Parquet partition.
//!
//! This is the Rust port of `lake/coverage/service.py`, including its
//! listings-aware classification and its point-in-time discipline: only
//! listing episodes known on or before `as_of` are consulted, so an as-of query
//! never exposes a listing, delisting, or rename dated after the as-of instant.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

use lake_catalog::{
    BarSource, CatalogRepository, CoverageQuery, CoverageRecord, CoverageStatus, ListingRecord,
    ListingRepository,
};
use lake_contracts::{symbol_filename, AssetClass, DataType, Date, DateTime, ExchangeSession};

use crate::error::CoverageRequestError;

/// Classification of one expected symbol/data-type session.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum CoverageState {
    /// No catalog row and no listing covers the session.
    Missing,
    /// The provider reported no data for the session.
    Empty,
    /// The session is currently trading, or the write was truncated.
    Partial,
    /// The session was fully fetched.
    Complete,
    /// The fetch failed and remains repairable.
    Error,
    /// A known listing episode does not cover the session.
    Delisted,
}

impl CoverageState {
    /// The persisted string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::Missing => "missing",
            Self::Empty => "empty",
            Self::Partial => "partial",
            Self::Complete => "complete",
            Self::Error => "error",
            Self::Delisted => "delisted",
        }
    }

    /// Parse the persisted string value.
    pub fn from_value(value: &str) -> Option<Self> {
        match value {
            "missing" => Some(Self::Missing),
            "empty" => Some(Self::Empty),
            "partial" => Some(Self::Partial),
            "complete" => Some(Self::Complete),
            "error" => Some(Self::Error),
            "delisted" => Some(Self::Delisted),
            _ => None,
        }
    }
}

impl fmt::Display for CoverageState {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.value())
    }
}

/// A UTC session window, including whether it is currently open.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CoverageSession {
    /// UTC calendar date of the session.
    pub session_date: Date,
    /// Inclusive UTC open.
    pub open_time: DateTime,
    /// Exclusive UTC close.
    pub close_time: DateTime,
    /// Whether `as_of` falls inside the trading window.
    pub is_partial: bool,
}

impl CoverageSession {
    /// Build a session window, rejecting a reversed open/close pair.
    ///
    /// Times are UTC by construction, so the Python timezone-awareness checks
    /// cannot fail here; only `open_time < close_time` is enforced.
    pub fn new(
        session_date: Date,
        open_time: DateTime,
        close_time: DateTime,
        is_partial: bool,
    ) -> Result<Self, CoverageRequestError> {
        if open_time >= close_time {
            return Err(CoverageRequestError::new(
                "invalid_session",
                "open_time must be before close_time",
                None,
            ));
        }
        Ok(Self {
            session_date,
            open_time,
            close_time,
            is_partial,
        })
    }

    /// Build a coverage session from an exchange session clock.
    ///
    /// Mirrors the Python session clock: a session is partial while
    /// `open_time <= as_of < close_time`.
    pub fn from_exchange(session: &ExchangeSession, as_of: DateTime) -> Self {
        Self {
            session_date: session.session_date,
            open_time: session.open_time,
            close_time: session.close_time,
            is_partial: lake_contracts::is_partial(session, as_of),
        }
    }
}

/// Coverage classification for one symbol, data type, and session.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SessionCoverage {
    /// Asset class of the expected session.
    pub asset_class: AssetClass,
    /// Data type of the expected session.
    pub data_type: DataType,
    /// Canonical uppercase symbol.
    pub symbol: String,
    /// The session window the entry describes.
    pub session: CoverageSession,
    /// Classification of the session.
    pub state: CoverageState,
    /// The persisted catalog row, when one exists.
    pub record: Option<CoverageRecord>,
}

impl SessionCoverage {
    /// Return the date used by the deterministic catalog and object key.
    pub fn session_date(&self) -> Date {
        self.session.session_date
    }

    /// Return the persisted catalog status, if a row exists.
    pub fn status(&self) -> Option<CoverageStatus> {
        self.record.as_ref().map(|record| record.status)
    }

    /// Whether this session has complete, non-empty coverage.
    ///
    /// A persisted `complete` row stays complete even when a currently open
    /// session reclassifies the entry as partial.
    pub fn is_complete(&self) -> bool {
        self.state == CoverageState::Complete
            || self
                .record
                .as_ref()
                .is_some_and(|record| record.status == CoverageStatus::Complete)
    }

    /// Whether no ingest retry is required for this session.
    pub fn is_terminal(&self) -> bool {
        matches!(
            self.state,
            CoverageState::Complete | CoverageState::Empty | CoverageState::Delisted
        )
    }

    /// Whether this entry belongs in an ingest gap plan.
    pub fn is_actionable(&self) -> bool {
        !self.is_terminal()
    }

    /// Whether a persisted partial row should be replaced by the fetch.
    pub fn replace_existing(&self) -> bool {
        self.state == CoverageState::Partial && self.record.is_some()
    }
}

/// Deterministically ordered classifications for an expected range.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CoverageReport {
    /// Asset class of the planned range.
    pub asset_class: AssetClass,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
    /// Classifications in data-type, symbol, then session order.
    pub entries: Vec<SessionCoverage>,
}

impl CoverageReport {
    /// Return entries in report order for one classification.
    pub fn by_state(&self, state: CoverageState) -> Vec<&SessionCoverage> {
        self.entries
            .iter()
            .filter(|entry| entry.state == state)
            .collect()
    }

    /// Return the missing entries in report order.
    pub fn missing(&self) -> Vec<&SessionCoverage> {
        self.by_state(CoverageState::Missing)
    }

    /// Return the empty entries in report order.
    pub fn empty(&self) -> Vec<&SessionCoverage> {
        self.by_state(CoverageState::Empty)
    }

    /// Return the partial entries in report order.
    pub fn partial(&self) -> Vec<&SessionCoverage> {
        self.by_state(CoverageState::Partial)
    }

    /// Return the complete entries in report order.
    pub fn complete(&self) -> Vec<&SessionCoverage> {
        self.by_state(CoverageState::Complete)
    }

    /// Return the errored entries in report order.
    pub fn errors(&self) -> Vec<&SessionCoverage> {
        self.by_state(CoverageState::Error)
    }

    /// Return the delisted entries in report order.
    pub fn delisted(&self) -> Vec<&SessionCoverage> {
        self.by_state(CoverageState::Delisted)
    }

    /// Whether all expected sessions are complete or terminally empty.
    pub fn is_satisfied(&self) -> bool {
        self.entries.iter().all(SessionCoverage::is_terminal)
    }

    /// Return the actionable entries without recalculating the report.
    pub fn gap_plan(&self) -> GapPlan {
        GapPlan {
            asset_class: self.asset_class,
            start_date: self.start_date,
            end_date: self.end_date,
            gaps: self
                .entries
                .iter()
                .filter(|entry| entry.is_actionable())
                .cloned()
                .collect(),
        }
    }
}

/// Actionable coverage entries for one asset class and inclusive range.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GapPlan {
    /// Asset class of the planned range.
    pub asset_class: AssetClass,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
    /// Actionable entries in report order.
    pub gaps: Vec<SessionCoverage>,
}

impl GapPlan {
    /// Return the missing gaps in plan order.
    pub fn missing(&self) -> Vec<&SessionCoverage> {
        self.gaps
            .iter()
            .filter(|entry| entry.state == CoverageState::Missing)
            .collect()
    }

    /// Return the partial gaps in plan order.
    pub fn partial(&self) -> Vec<&SessionCoverage> {
        self.gaps
            .iter()
            .filter(|entry| entry.state == CoverageState::Partial)
            .collect()
    }

    /// Return the errored gaps in plan order.
    pub fn errors(&self) -> Vec<&SessionCoverage> {
        self.gaps
            .iter()
            .filter(|entry| entry.state == CoverageState::Error)
            .collect()
    }
}

/// Return inclusive equity or crypto sessions with current-session flags.
///
/// The XNYS calendar supplies equity sessions and UTC days supply crypto
/// sessions; any other asset class is rejected with the Python message.
pub fn sessions_for(
    asset_class: AssetClass,
    start_date: Date,
    end_date: Date,
    as_of: DateTime,
) -> Result<Vec<CoverageSession>, CoverageRequestError> {
    validate_date_range(start_date, end_date)?;
    if !matches!(
        asset_class,
        AssetClass::Equity | AssetClass::Crypto | AssetClass::Option
    ) {
        return Err(CoverageRequestError::new(
            "unsupported_asset_class",
            "coverage planning supports equity and crypto only",
            None,
        ));
    }
    let sessions = lake_contracts::sessions_for(asset_class, start_date, end_date)
        .map_err(CoverageRequestError::from)?;
    Ok(sessions
        .iter()
        .map(|session| CoverageSession::from_exchange(session, as_of))
        .collect())
}

/// Classify every expected symbol/data-type session in a calendar range.
///
/// Missing catalog rows are `missing` for closed sessions and `partial` for a
/// currently open session. `empty` is retained as a distinct, terminal result.
/// Existing partial and error rows remain actionable, while a complete row for
/// a currently open session is deliberately reclassified as partial so the
/// canonical object can be replaced after more data arrives.
///
/// When a listings repository is supplied, coverage becomes listings aware: a
/// symbol with no listing episode covering a session is classified `delisted`
/// instead of `missing`, keeping sessions before a listing or after a delisting
/// out of the gap plan. Persisted catalog rows keep their own classification
/// regardless of listing knowledge.
///
/// `interval` selects the bars interval dimension: bars rows are keyed by
/// their canonical token, and every other data type is matched with `None`.
#[allow(clippy::too_many_arguments)]
pub fn calculate_coverage(
    catalog: &dyn CatalogRepository,
    listings: Option<&dyn ListingRepository>,
    asset_class: AssetClass,
    data_types: &[DataType],
    symbols: &[String],
    interval: Option<&str>,
    start_date: Date,
    end_date: Date,
    as_of: DateTime,
) -> Result<CoverageReport, CoverageRequestError> {
    validate_date_range(start_date, end_date)?;
    let normalized_symbols = normalize_symbols(symbols)?;
    let normalized_data_types = normalize_data_types(data_types)?;
    let sessions = sessions_for(asset_class, start_date, end_date, as_of)?;

    let mut datasets = normalized_data_types.clone();
    if datasets.contains(&DataType::Bars) && !datasets.contains(&DataType::Trade) {
        datasets.push(DataType::Trade);
    }
    let mut records = Vec::new();
    for symbol in &normalized_symbols {
        for data_type in &datasets {
            records.extend(
                catalog
                    .list(&CoverageQuery {
                        asset_class: Some(asset_class),
                        data_type: Some(*data_type),
                        symbol: Some(symbol.clone()),
                        interval: if *data_type == DataType::Bars {
                            interval.map(str::to_owned)
                        } else {
                            None
                        },
                        start: Some(start_date),
                        end: Some(end_date),
                    })
                    .map_err(CoverageRequestError::from)?,
            );
        }
    }
    let mut records_by_key: BTreeMap<(DataType, Option<&str>, &str, Date), &CoverageRecord> =
        BTreeMap::new();
    for record in &records {
        records_by_key.insert(
            (
                record.data_type,
                record.interval.as_deref(),
                record.symbol.as_str(),
                record.session_date,
            ),
            record,
        );
    }

    let visible_listings = visible_listings_by_symbol(listings, &normalized_symbols, as_of.date())?;

    let mut entries = Vec::new();
    for data_type in &normalized_data_types {
        let requested_interval = if *data_type == DataType::Bars {
            interval
        } else {
            None
        };
        for symbol in &normalized_symbols {
            let episodes = visible_listings.get(symbol).map_or(&[][..], Vec::as_slice);
            for session in &sessions {
                let record = records_by_key
                    .get(&(
                        *data_type,
                        requested_interval,
                        symbol.as_str(),
                        session.session_date,
                    ))
                    .copied();
                // A derived bars row is only current while its recorded trade
                // revision matches the session's live trade partition; a trade
                // repair replaces that partition's checksum and invalidates
                // the derived bars on the next classification.
                let trade_revision = if *data_type == DataType::Bars {
                    records_by_key
                        .get(&(DataType::Trade, None, symbol.as_str(), session.session_date))
                        .copied()
                        .and_then(|trade| trade.checksum.as_deref())
                } else {
                    None
                };
                let mut state = coverage_state(record, session, trade_revision);
                if record.is_none()
                    && !episodes.is_empty()
                    && !listing_covers(episodes, session.session_date, as_of.date())
                {
                    state = CoverageState::Delisted;
                }
                entries.push(SessionCoverage {
                    asset_class,
                    data_type: *data_type,
                    symbol: symbol.clone(),
                    session: *session,
                    state,
                    record: record.cloned(),
                });
            }
        }
    }

    Ok(CoverageReport {
        asset_class,
        start_date,
        end_date,
        entries,
    })
}

/// Return whether a session is complete, terminally empty, or repairable.
///
/// Precedence mirrors Python: a persisted `empty` row wins even for an open
/// session; otherwise an open session is `partial`; otherwise a missing row is
/// `missing`; otherwise the persisted status is kept.
///
/// Bars provenance adds one revision rule: a `derived` bars row is complete
/// (or terminally empty) only while its recorded `source_revision` still
/// matches the current trade partition revision. A mismatch means a trade
/// repair replaced the derivation input, so the row is reclassified `partial`
/// and re-derived or replaced by provider bars on the next ensure. Provider
/// bars retain their recorded response revision; persisted terminal bars require provenance.
fn coverage_state(
    record: Option<&CoverageRecord>,
    session: &CoverageSession,
    trade_revision: Option<&str>,
) -> CoverageState {
    if let Some(record) = record {
        if record.status == CoverageStatus::Empty && !derived_bars_stale(record, trade_revision) {
            return CoverageState::Empty;
        }
    }
    if session.is_partial {
        return CoverageState::Partial;
    }
    match record {
        None => CoverageState::Missing,
        Some(record) => match record.status {
            CoverageStatus::Complete => {
                if derived_bars_stale(record, trade_revision) {
                    CoverageState::Partial
                } else {
                    CoverageState::Complete
                }
            }
            CoverageStatus::Empty => {
                if derived_bars_stale(record, trade_revision) {
                    CoverageState::Partial
                } else {
                    CoverageState::Empty
                }
            }
            CoverageStatus::Partial => CoverageState::Partial,
            CoverageStatus::Error => CoverageState::Error,
        },
    }
}

/// Whether a derived bars row no longer matches its trade partition revision.
fn derived_bars_stale(record: &CoverageRecord, trade_revision: Option<&str>) -> bool {
    record.data_type == DataType::Bars
        && record.bar_source == Some(BarSource::Derived)
        && record.source_revision.as_deref() != trade_revision
}

/// Return listing episodes known on `as_of` keyed by canonical symbol.
///
/// Episodes listed after `as_of` are invisible: their existence, and any
/// delisting or rename they carry, cannot surface in an as-of query. The
/// repository's `as_of` filter already applies `is_known`; the extra check
/// keeps the Python behavior even for adapters that filter differently.
fn visible_listings_by_symbol(
    listings: Option<&dyn ListingRepository>,
    symbols: &[String],
    as_of_day: Date,
) -> Result<BTreeMap<String, Vec<ListingRecord>>, CoverageRequestError> {
    let Some(listings) = listings else {
        return Ok(BTreeMap::new());
    };
    let mut visible = BTreeMap::new();
    for symbol in symbols {
        let episodes = listings
            .list(None, Some(symbol.as_str()), Some(as_of_day))
            .map_err(CoverageRequestError::from)?;
        let known: Vec<ListingRecord> = episodes
            .into_iter()
            .filter(|record| record.is_known(as_of_day))
            .collect();
        if !known.is_empty() {
            visible.insert(symbol.clone(), known);
        }
    }
    Ok(visible)
}

/// Return whether any known episode covered the session date.
fn listing_covers(episodes: &[ListingRecord], session_date: Date, as_of_day: Date) -> bool {
    episodes
        .iter()
        .any(|record| record.active_on(session_date, as_of_day))
}

/// Canonicalise, dedupe, and sort the requested symbols.
///
/// Mirrors `_normalize_symbols`: every symbol is stripped and uppercased, the
/// result is deduplicated and sorted, empty input is rejected, and every
/// symbol is validated as a path-safe lake symbol.
fn normalize_symbols(symbols: &[String]) -> Result<Vec<String>, CoverageRequestError> {
    let mut normalized: BTreeSet<String> = BTreeSet::new();
    for value in symbols {
        normalized.insert(value.trim().to_uppercase());
    }
    if normalized.is_empty() || normalized.iter().any(String::is_empty) {
        return Err(CoverageRequestError::new(
            "invalid_symbol",
            "symbols must contain non-empty strings",
            None,
        ));
    }
    for symbol in &normalized {
        symbol_filename(symbol).map_err(|error| {
            CoverageRequestError::new("invalid_symbol", error.to_string(), None)
        })?;
    }
    Ok(normalized.into_iter().collect())
}

/// Dedupe the requested data types and sort them by their persisted value.
///
/// The string order is the Python one: `quote` precedes `trade`, while the
/// Rust enum order would not.
fn normalize_data_types(data_types: &[DataType]) -> Result<Vec<DataType>, CoverageRequestError> {
    let mut normalized: Vec<DataType> = data_types.to_vec();
    normalized.sort_by_key(|data_type| data_type.value());
    normalized.dedup();
    if normalized.is_empty() {
        return Err(CoverageRequestError::new(
            "invalid_data_types",
            "data_types must not be empty",
            None,
        ));
    }
    Ok(normalized)
}

/// Reject a reversed inclusive date range with the Python message.
fn validate_date_range(start_date: Date, end_date: Date) -> Result<(), CoverageRequestError> {
    if start_date > end_date {
        return Err(CoverageRequestError::new(
            "invalid_date_range",
            "start must be on or before end",
            None,
        ));
    }
    Ok(())
}
