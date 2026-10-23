//! Deterministic gap detection against expected market-lake coverage.
//!
//! Port of the detection half of `harness.data.gap_healing`: a live feed can
//! drop messages, so the detector classifies silences against calendar-aware
//! expected coverage. All time comes from event timestamps, an explicit `now`,
//! or an injected clock. Backfill traffic runs through `harness-lakeclient` at
//! the live boundary, so no file or path surface lives here.

use std::collections::HashMap;
use std::rc::Rc;

use crate::error::DataError;
use crate::events::MarketEvent;
use crate::lake::sessions_for;
use crate::lake::AssetClass;
use crate::lake::DataType;
use crate::lake::ExchangeSession;
use crate::symbol::canonical_symbol;
use crate::time::Date;
use crate::time::DateTime;
use crate::time::Nanos;

/// One hole in a live feed's expected coverage, half-open in time.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CoverageGap {
    /// Canonical symbol.
    pub symbol: String,
    /// Data type.
    pub data_type: DataType,
    /// Last live-delivered moment, exclusive in the healed slice.
    pub start: DateTime,
    /// Moment live flow resumes, exclusive.
    pub end: DateTime,
}

impl CoverageGap {
    /// Build a validated gap.
    pub fn new(
        symbol: &str,
        data_type: DataType,
        start: DateTime,
        end: DateTime,
    ) -> Result<Self, DataError> {
        if start >= end {
            return Err(DataError::value("gap start must precede gap end"));
        }
        Ok(Self {
            symbol: canonical_symbol(symbol)?,
            data_type,
            start,
            end,
        })
    }

    /// The `(symbol, data_type)` partition this gap belongs to.
    pub fn partition(&self) -> (String, DataType) {
        (self.symbol.clone(), self.data_type)
    }

    /// The half-open interval length of the gap.
    pub fn duration(&self) -> Nanos {
        self.end.to_nanos() - self.start.to_nanos()
    }
}

/// Expected-coverage windows for one asset class.
pub trait CoverageCalendar {
    /// The sessions whose windows overlap the inclusive date range.
    fn sessions(&self, start: Date, end: Date) -> Result<Vec<ExchangeSession>, DataError>;
}

/// Expected coverage from the lake session contract, without any clock.
#[derive(Clone, Copy, Debug)]
pub struct LakeSessionCalendar {
    asset_class: AssetClass,
}

impl LakeSessionCalendar {
    /// Build a calendar for an equity or crypto asset class.
    pub fn new(asset_class: AssetClass) -> Result<Self, DataError> {
        if !matches!(asset_class, AssetClass::Equity | AssetClass::Crypto) {
            return Err(DataError::Unsupported(format!(
                "gap healing does not support {} data",
                asset_class.value()
            )));
        }
        Ok(Self { asset_class })
    }

    /// The asset class whose sessions this calendar resolves.
    pub fn asset_class(&self) -> AssetClass {
        self.asset_class
    }
}

impl CoverageCalendar for LakeSessionCalendar {
    fn sessions(&self, start: Date, end: Date) -> Result<Vec<ExchangeSession>, DataError> {
        sessions_for(self.asset_class, start, end).map_err(Into::into)
    }
}

/// Classify live-feed silences as gaps against expected coverage.
pub struct LiveGapDetector {
    calendar: Rc<dyn CoverageCalendar>,
    last_seen: HashMap<(String, DataType), DateTime>,
    dirty_through: HashMap<(String, DataType), DateTime>,
}

impl LiveGapDetector {
    /// Build a detector over `calendar`.
    pub fn new(calendar: Rc<dyn CoverageCalendar>) -> Self {
        Self {
            calendar,
            last_seen: HashMap::new(),
            dirty_through: HashMap::new(),
        }
    }

    /// The expected-coverage calendar.
    pub fn calendar(&self) -> &Rc<dyn CoverageCalendar> {
        &self.calendar
    }

    /// The tracked partitions in deterministic order.
    pub fn tracked(&self) -> Vec<(String, DataType)> {
        let mut partitions: Vec<(String, DataType)> = self.last_seen.keys().cloned().collect();
        partitions.sort_by(partition_key);
        partitions
    }

    /// The newest live timestamp observed for one partition.
    pub fn last_seen(&self, symbol: &str, data_type: DataType) -> Option<DateTime> {
        let symbol = canonical_symbol(symbol).ok()?;
        self.last_seen.get(&(symbol, data_type)).copied()
    }

    /// Record one delivered live event and return the partition head.
    ///
    /// # Errors
    ///
    /// Returns [`DataError::Unsupported`] for bar events: bars are a lake-built
    /// dataset partitioned by interval, and live-feed gap healing never tracks
    /// them as tick partitions.
    pub fn observe(&mut self, event: &MarketEvent) -> Result<DateTime, DataError> {
        let partition = event_partition(event).ok_or_else(|| {
            DataError::Unsupported(
                "gap healing does not track bars partitions: bars are lake-built, not live ticks"
                    .to_string(),
            )
        })?;
        let current = self.last_seen.get(&partition).copied();
        if current.is_none_or(|value| event.timestamp() > value) {
            self.last_seen.insert(partition.clone(), event.timestamp());
        }
        Ok(self.last_seen[&partition])
    }

    /// Extend the interval that must not be detected as a new gap.
    pub fn mark_dirty(&mut self, gap: &CoverageGap) {
        let partition = gap.partition();
        let current = self.dirty_through.get(&partition).copied();
        if current.is_none_or(|value| gap.end > value) {
            self.dirty_through.insert(partition, gap.end);
        }
    }

    /// Gaps for tracked partitions silent for at least `stale_after` nanos.
    pub fn detect(
        &self,
        now: DateTime,
        stale_after: Nanos,
        symbols: Option<&[String]>,
    ) -> Result<Vec<CoverageGap>, DataError> {
        if stale_after <= 0 {
            return Err(DataError::value("stale_after must be positive"));
        }
        let head = now;
        let mut gaps = Vec::new();
        for partition in self.selected(symbols) {
            let Some(last) = self.last_seen.get(&partition).copied() else {
                continue;
            };
            if head.to_nanos() - last.to_nanos() < stale_after {
                continue;
            }
            gaps.extend(self.fresh(&partition, last, head)?);
        }
        Ok(gaps)
    }

    /// Gaps for one partition inside `[start, end)`, dirty-clipped.
    pub fn detect_between(
        &self,
        symbol: &str,
        data_type: DataType,
        start: DateTime,
        end: DateTime,
    ) -> Result<Vec<CoverageGap>, DataError> {
        let partition = (canonical_symbol(symbol)?, data_type);
        self.fresh(&partition, start, end)
    }

    fn fresh(
        &self,
        partition: &(String, DataType),
        start: DateTime,
        end: DateTime,
    ) -> Result<Vec<CoverageGap>, DataError> {
        if start >= end {
            return Ok(Vec::new());
        }
        let mut start = start;
        if let Some(dirty) = self.dirty_through.get(partition).copied() {
            if dirty > start {
                start = dirty;
                if start >= end {
                    return Ok(Vec::new());
                }
            }
        }
        let (symbol, data_type) = partition;
        let mut gaps = Vec::new();
        for (window_start, window_end) in self.coverage_windows(start, end)? {
            gaps.push(CoverageGap::new(
                symbol,
                *data_type,
                window_start,
                window_end,
            )?);
        }
        Ok(gaps)
    }

    fn coverage_windows(
        &self,
        start: DateTime,
        end: DateTime,
    ) -> Result<Vec<(DateTime, DateTime)>, DataError> {
        let padding = crate::time::Date::from_days(1);
        let sessions = self.calendar.sessions(
            start.date().add_days(-padding.days()),
            end.date().add_days(padding.days()),
        )?;
        let mut windows = Vec::new();
        for session in sessions {
            let window_start = start.max(session.open_time);
            let window_end = end.min(session.close_time);
            if window_start < window_end {
                windows.push((window_start, window_end));
            }
        }
        Ok(windows)
    }

    fn selected(&self, symbols: Option<&[String]>) -> Vec<(String, DataType)> {
        let tracked = self.tracked();
        match symbols {
            None => tracked,
            Some(symbols) => {
                let wanted: std::collections::HashSet<String> = symbols
                    .iter()
                    .filter_map(|symbol| canonical_symbol(symbol).ok())
                    .collect();
                tracked
                    .into_iter()
                    .filter(|partition| wanted.contains(&partition.0))
                    .collect()
            }
        }
    }
}

fn partition_key(a: &(String, DataType), b: &(String, DataType)) -> std::cmp::Ordering {
    (a.0.as_str(), a.1.value()).cmp(&(b.0.as_str(), b.1.value()))
}

/// The tracked `(symbol, data_type)` partition for one live event.
///
/// Bars are explicitly not a gap-healing partition: the detector returns
/// `None` so [`LiveGapDetector::observe`] fails loud instead of filing a bar
/// under the trade partition.
fn event_partition(event: &MarketEvent) -> Option<(String, DataType)> {
    let data_type = match event.event_type() {
        crate::events::EventType::Quote => DataType::Quote,
        crate::events::EventType::Trade => DataType::Trade,
        crate::events::EventType::Bar => return None,
        _ => DataType::Trade,
    };
    Some((event.symbol().to_string(), data_type))
}
