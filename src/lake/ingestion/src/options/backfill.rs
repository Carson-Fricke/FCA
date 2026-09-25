//! Deterministic, resumable planning for option front-set backfills.
//!
//! Ports `lake.ingestion.options.backfill`. The coverage catalog is the resume
//! checkpoint: a later call selects and replaces the same front-set key, then
//! omits OSI symbol-days whose requested trade and quote rows are already
//! `complete` or terminal `empty`. Spot values are caller-supplied so planning
//! remains deterministic and offline.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use lake_catalog::{
    CatalogRepository, ContractRecord, ContractsCatalog, CoverageQuery, CoverageRecord,
    CoverageStatus, DailyFrontSet,
};
use lake_configuration::OptionsUniverse;
use lake_contracts::{xnys_sessions, AssetClass, DataError, DataType, Date, DateTime, Decimal18};
use lake_coverage::CoverageSession;
use serde_json::{json, Value};

/// The injected UTC clock used instead of `as_of`.
pub type OptionClock = Arc<dyn Fn() -> DateTime + Send + Sync>;

/// Resolve the underlying spot used for one daily front-set selection.
pub trait OptionSpotProvider: Send + Sync {
    /// Return an exact positive spot value for one underlying and date.
    fn spot(&self, underlying: &str, selection_date: Date) -> Result<Decimal18, DataError>;
}

/// Caller-supplied spot values: a fixed table or a resolving provider.
pub enum SpotValues {
    /// One exact spot per canonical underlying.
    Map(BTreeMap<String, Decimal18>),
    /// A resolving provider, invoked per underlying and session date.
    Provider(Arc<dyn OptionSpotProvider>),
}

impl SpotValues {
    /// Resolve the spot for one underlying and session date.
    fn resolve(&self, underlying: &str, session_date: Date) -> Result<Decimal18, DataError> {
        match self {
            Self::Map(map) => map.get(underlying).copied().ok_or_else(|| {
                DataError::value(format!("spot is missing for underlying {underlying}"))
            }),
            Self::Provider(provider) => provider.spot(underlying, session_date),
        }
    }
}

/// One selected OSI symbol-day request for the shared queue worker.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OptionBackfillItem {
    /// The selected contract.
    pub contract: ContractRecord,
    /// The XNYS session to fetch.
    pub session: CoverageSession,
    /// Requested data types, canonical order.
    pub data_types: Vec<DataType>,
    /// Whether a persisted `partial` row must be replaced.
    pub replace_existing: bool,
}

impl OptionBackfillItem {
    /// The selected OSI symbol.
    pub fn symbol(&self) -> &str {
        &self.contract.symbol
    }

    /// The XNYS session date.
    pub fn session_date(&self) -> Date {
        self.session.session_date
    }

    /// The complete provider-independent worker payload.
    pub fn queue_message(&self) -> Value {
        json!({
            "asset_class": AssetClass::Option.value(),
            "data_types": self
                .data_types
                .iter()
                .map(|data_type| data_type.value())
                .collect::<Vec<_>>(),
            "end_time": format_datetime(self.session.close_time),
            "expiration_date": self.contract.expiration_date.isoformat(),
            "option_type": self.contract.option_type.value(),
            "replace_existing": self.replace_existing,
            "session_date": self.session_date().isoformat(),
            "start_time": format_datetime(self.session.open_time),
            "strike_price": self.contract.strike_price.to_canonical_string(),
            "symbol": self.contract.symbol,
            "underlying": self.contract.underlying,
        })
    }
}

/// Daily front-set work in deterministic session, underlying, OSI order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OptionBackfillPlan {
    /// Inclusive first planned date.
    pub start: Date,
    /// Inclusive last planned date.
    pub end: Date,
    /// Selected OSI symbol-days in dispatch order.
    pub items: Vec<OptionBackfillItem>,
    /// Persisted front sets in session, underlying order.
    pub front_sets: Vec<DailyFrontSet>,
    /// Every actionable OSI symbol-day before the work limit applied.
    pub available_items: usize,
}

impl OptionBackfillPlan {
    /// Build a validated plan.
    pub fn new(
        start: Date,
        end: Date,
        items: Vec<OptionBackfillItem>,
        front_sets: Vec<DailyFrontSet>,
        available_items: usize,
    ) -> Result<Self, DataError> {
        if start > end {
            return Err(DataError::value("start must be on or before end"));
        }
        if available_items < items.len() {
            return Err(DataError::value(
                "available_items must include the selected items",
            ));
        }
        Ok(Self {
            start,
            end,
            items,
            front_sets,
            available_items,
        })
    }

    /// The selected items.
    pub fn items(&self) -> &[OptionBackfillItem] {
        &self.items
    }

    /// Whether a work limit omitted remaining actionable OSI symbol-days.
    pub fn has_more(&self) -> bool {
        self.items.len() < self.available_items
    }

    /// Distinct session dates in dispatch order.
    pub fn session_dates(&self) -> Vec<Date> {
        let mut dates: Vec<Date> = Vec::new();
        for item in &self.items {
            if !dates.contains(&item.session_date()) {
                dates.push(item.session_date());
            }
        }
        dates
    }
}

/// The planning inputs for one option backfill window.
pub struct OptionBackfillRequest<'a> {
    /// Underlyings to plan; `None` selects the universe's soak underlyings.
    pub underlyings: Option<&'a [String]>,
    /// The typed options universe, when one is configured.
    pub universe: Option<&'a OptionsUniverse>,
    /// Caller-supplied spot values.
    pub spots: SpotValues,
    /// Requested data types; `None` selects trade and quote.
    pub data_types: Option<&'a [DataType]>,
    /// Inclusive first date.
    pub start: Date,
    /// Inclusive last date.
    pub end: Date,
    /// The deterministic "now"; `None` uses `clock` or the system clock.
    pub as_of: Option<DateTime>,
    /// An injected clock; rejected together with `as_of`.
    pub clock: Option<OptionClock>,
    /// Work limit; `None` plans every actionable OSI symbol-day.
    pub max_items: Option<usize>,
}

/// Persist daily front sets and plan only non-terminal option coverage.
pub fn plan_option_backfill(
    catalog: &ContractsCatalog,
    coverage: &dyn CatalogRepository,
    selector: &crate::options::selector::OptionChainSelector,
    request: OptionBackfillRequest<'_>,
) -> Result<OptionBackfillPlan, DataError> {
    if request.max_items.is_some_and(|max_items| max_items < 1) {
        return Err(DataError::value("max_items must be positive"));
    }
    let underlyings = configuration_underlyings(request.underlyings, request.universe)?;
    let selected_data_types = normalize_data_types(request.data_types)?;
    let current_time = resolve_clock(request.as_of, request.clock.as_ref())?;
    let sessions: Vec<CoverageSession> = xnys_sessions(request.start, request.end)?
        .iter()
        .map(|session| CoverageSession::from_exchange(session, current_time))
        .filter(|session| session.close_time <= current_time)
        .collect();
    let coverage_by_key = coverage_index(coverage, request.start, request.end)?;

    let mut front_sets: Vec<DailyFrontSet> = Vec::new();
    let mut items: Vec<OptionBackfillItem> = Vec::new();
    for session in &sessions {
        for underlying in &underlyings {
            let spot = request.spots.resolve(underlying, session.session_date)?;
            let contracts = selector.select_from_repository(
                catalog.contracts.as_ref(),
                underlying,
                session.session_date,
                spot,
            )?;
            let front_set = DailyFrontSet::new(underlying, session.session_date, contracts)?;
            if request
                .universe
                .is_none_or(|universe| universe.selection.persist_front_set)
            {
                catalog.upsert_front_set(&front_set)?;
            }
            front_sets.push(front_set.clone());
            for contract in &front_set.contracts {
                if is_terminal(
                    &coverage_by_key,
                    &contract.symbol,
                    session.session_date,
                    &selected_data_types,
                ) {
                    continue;
                }
                items.push(OptionBackfillItem {
                    contract: contract.clone(),
                    session: *session,
                    data_types: selected_data_types.clone(),
                    replace_existing: replace_existing(
                        &coverage_by_key,
                        &contract.symbol,
                        session.session_date,
                        &selected_data_types,
                    ),
                });
            }
        }
    }

    let available_items = items.len();
    let items = match request.max_items {
        None => items,
        Some(max_items) => items.into_iter().take(max_items).collect(),
    };
    OptionBackfillPlan::new(
        request.start,
        request.end,
        items,
        front_sets,
        available_items,
    )
}

/// Dispatch option work through the existing queue sender boundary.
pub trait QueueSender: Send + Sync {
    /// Send one queue message.
    fn send(&self, message: &Value) -> Result<(), DataError>;
}

/// Send every plan item's queue message, returning how many were sent.
pub fn enqueue_option_backfill(
    plan: &OptionBackfillPlan,
    sender: &dyn QueueSender,
) -> Result<usize, DataError> {
    for item in plan.items() {
        sender.send(&item.queue_message())?;
    }
    Ok(plan.items().len())
}

/// Normalize the underlyings list: canonicalize, deduplicate, sort.
fn configuration_underlyings(
    underlyings: Option<&[String]>,
    universe: Option<&OptionsUniverse>,
) -> Result<Vec<String>, DataError> {
    let values: &[String] = match (underlyings, universe) {
        (Some(values), _) => values,
        (None, Some(universe)) => &universe.soak.underlyings,
        (None, None) => {
            return Err(DataError::value("underlyings or universe must be supplied"));
        }
    };
    let normalized: BTreeSet<String> = values
        .iter()
        .map(|value| value.trim().to_uppercase())
        .collect();
    if normalized.contains(&String::new()) {
        return Err(DataError::value(
            "underlyings must contain non-empty strings",
        ));
    }
    if normalized.is_empty() {
        return Err(DataError::value(
            "underlyings must contain non-empty strings",
        ));
    }
    Ok(normalized.into_iter().collect())
}

/// Return the requested data types in canonical trade-then-quote order.
fn normalize_data_types(values: Option<&[DataType]>) -> Result<Vec<DataType>, DataError> {
    const CANONICAL: [DataType; 2] = [DataType::Trade, DataType::Quote];
    let Some(values) = values else {
        return Ok(CANONICAL.to_vec());
    };
    if values.is_empty() {
        return Err(DataError::value("data_types must not be empty"));
    }
    if values.iter().any(|value| !CANONICAL.contains(value)) {
        return Err(DataError::value(
            "option data_types must be trades or quotes",
        ));
    }
    Ok(CANONICAL
        .into_iter()
        .filter(|data_type| values.contains(data_type))
        .collect())
}

/// Resolve the "now" instant, rejecting two time sources.
fn resolve_clock(
    as_of: Option<DateTime>,
    clock: Option<&OptionClock>,
) -> Result<DateTime, DataError> {
    match (as_of, clock) {
        (Some(_), Some(_)) => Err(DataError::value(
            "as_of and clock must not both be supplied",
        )),
        (Some(as_of), None) => Ok(as_of),
        (None, Some(clock)) => Ok(clock()),
        (None, None) => Ok(system_now()),
    }
}

/// The production UTC clock: now in microseconds since the Unix epoch.
fn system_now() -> DateTime {
    match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        Ok(elapsed) => {
            DateTime::from_micros(i64::try_from(elapsed.as_micros()).unwrap_or(i64::MAX))
        }
        Err(error) => {
            DateTime::from_micros(-i64::try_from(error.duration().as_micros()).unwrap_or(i64::MAX))
        }
    }
}

/// Index option coverage rows in `[start, end]` by symbol-day-type.
fn coverage_index(
    coverage: &dyn CatalogRepository,
    start: Date,
    end: Date,
) -> Result<BTreeMap<(String, Date, DataType), CoverageRecord>, DataError> {
    let records = coverage.list(&CoverageQuery {
        asset_class: Some(AssetClass::Option),
        data_type: None,
        symbol: None,
        start: Some(start),
        end: Some(end),
    })?;
    Ok(records
        .into_iter()
        .map(|record| {
            (
                (record.symbol.clone(), record.session_date, record.data_type),
                record,
            )
        })
        .collect())
}

/// Whether every requested data type already has terminal coverage.
fn is_terminal(
    coverage: &BTreeMap<(String, Date, DataType), CoverageRecord>,
    symbol: &str,
    session_date: Date,
    data_types: &[DataType],
) -> bool {
    data_types.iter().all(|data_type| {
        coverage
            .get(&(symbol.to_string(), session_date, *data_type))
            .is_some_and(|record| {
                matches!(
                    record.status,
                    CoverageStatus::Complete | CoverageStatus::Empty
                )
            })
    })
}

/// Whether any requested data type holds a persisted `partial` row.
fn replace_existing(
    coverage: &BTreeMap<(String, Date, DataType), CoverageRecord>,
    symbol: &str,
    session_date: Date,
    data_types: &[DataType],
) -> bool {
    data_types.iter().any(|data_type| {
        coverage
            .get(&(symbol.to_string(), session_date, *data_type))
            .is_some_and(|record| record.status == CoverageStatus::Partial)
    })
}

/// Format one session bound the way Python's `_format_datetime` does.
pub(crate) fn format_datetime(value: DateTime) -> String {
    value.isoformat_seconds_z()
}

#[cfg(test)]
#[path = "../../tests/unit/options_backfill.rs"]
mod tests;
