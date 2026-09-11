//! Deterministic healing of live-feed gaps through the market lake.
//!
//! Port of `harness.data.gap_healing`. A live feed can drop messages; the
//! healer classifies silences against calendar-aware expected coverage, fetches
//! the missing slice from the lake, and only then lets live flow resume. All
//! time comes from event timestamps, an explicit `now`, or an injected clock.

use std::collections::HashMap;
use std::fmt;
use std::rc::Rc;

use crate::clock::Clock;
use crate::error::DataError;
use crate::events::MarketEvent;
use crate::lake::{
    crypto_utc_days, xnys_sessions, AssetClass, CoverageState, DataType, DatasetRequest,
    EnsureResponse, ExchangeSession, FileQueryFile, LakeClientPort,
};
use crate::replay::{LakeReplayFeed, ParquetReader};
use crate::symbol::canonical_symbol;
use crate::time::{Date, DateTime, Nanos};

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
        if end < start {
            return Err(DataError::value("end must not be before start"));
        }
        match self.asset_class {
            AssetClass::Equity => xnys_sessions(start, end),
            _ => Ok(crypto_utc_days(start, end)?
                .into_iter()
                .map(|day| ExchangeSession {
                    session_date: day,
                    open_time: DateTime::midnight(day),
                    close_time: DateTime::midnight(day.add_days(1)),
                })
                .collect()),
        }
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
    pub fn observe(&mut self, event: &MarketEvent) -> DateTime {
        let partition = event_partition(event);
        let current = self.last_seen.get(&partition).copied();
        if current.is_none_or(|value| event.timestamp() > value) {
            self.last_seen.insert(partition.clone(), event.timestamp());
        }
        self.last_seen[&partition]
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

fn event_partition(event: &MarketEvent) -> (String, DataType) {
    let data_type = match event.event_type() {
        crate::events::EventType::Quote => DataType::Quote,
        _ => DataType::Trade,
    };
    (event.symbol().to_string(), data_type)
}

/// Outcome of one heal attempt recorded in the action trail.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HealingStatus {
    /// Missing events were replayed from the lake.
    Healed,
    /// The lake could not cover the gap.
    Uncovered,
}

/// Immutable audit record for one heal attempt over one gap interval.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HealingAction {
    /// The gap the attempt covered.
    pub gap: CoverageGap,
    /// Attempt outcome.
    pub status: HealingStatus,
    /// Number of admitted events.
    pub events: usize,
    /// Sessions overlapped by the gap.
    pub sessions: Vec<Date>,
    /// Human-readable detail.
    pub detail: String,
}

/// The market lake could not cover a detected gap without reordering.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UncoveredGapError {
    /// The interval that could not be healed.
    pub gap: CoverageGap,
    /// Why the lake could not cover it.
    pub reason: String,
    /// Events healed for earlier gaps in the same attempt.
    pub healed_events: Vec<MarketEvent>,
}

impl fmt::Display for UncoveredGapError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} {} [{}, {}): {}",
            self.gap.symbol,
            self.gap.data_type.value(),
            self.gap.start,
            self.gap.end,
            self.reason
        )
    }
}

impl std::error::Error for UncoveredGapError {}

/// Fetch one gap's missing slice from the lake through ensure and query.
pub struct LakeGapBackfiller {
    asset_class: AssetClass,
    calendar: Rc<dyn CoverageCalendar>,
    client: Rc<dyn LakeClientPort>,
    reader: Rc<dyn ParquetReader>,
    job_timeout: f64,
    job_poll_interval: f64,
}

impl LakeGapBackfiller {
    /// Build a backfiller over the published lake client and a file reader.
    pub fn new(
        client: Rc<dyn LakeClientPort>,
        reader: Rc<dyn ParquetReader>,
        calendar: Rc<dyn CoverageCalendar>,
        asset_class: AssetClass,
    ) -> Result<Self, DataError> {
        if !matches!(asset_class, AssetClass::Equity | AssetClass::Crypto) {
            return Err(DataError::Unsupported(format!(
                "gap healing does not support {} data",
                asset_class.value()
            )));
        }
        Ok(Self {
            asset_class,
            calendar,
            client,
            reader,
            job_timeout: 60.0,
            job_poll_interval: 0.5,
        })
    }

    /// The asset class requested from the lake.
    pub fn asset_class(&self) -> AssetClass {
        self.asset_class
    }

    /// Return the gap's missing events in timestamp order.
    pub fn backfill(&self, gap: &CoverageGap) -> Result<Vec<MarketEvent>, UncoveredGapError> {
        let sessions = self.expected_sessions(gap)?;
        let request = DatasetRequest {
            asset_class: self.asset_class,
            data_type: gap.data_type,
            symbol: Some(gap.symbol.clone()),
            start_date: sessions[0].session_date,
            end_date: sessions[sessions.len() - 1].session_date,
        };
        let coverage = self.ensure(&request);
        let covered = self.covered_sessions(gap, coverage.as_ref(), &sessions)?;
        let files = self.query(gap, &request, covered.as_deref(), &sessions)?;
        self.read(gap, &sessions, &files)
    }

    fn expected_sessions(
        &self,
        gap: &CoverageGap,
    ) -> Result<Vec<ExchangeSession>, UncoveredGapError> {
        let candidates = self
            .calendar
            .sessions(gap.start.date().add_days(-1), gap.end.date().add_days(1))
            .map_err(|error| UncoveredGapError {
                gap: gap.clone(),
                reason: error.to_string(),
                healed_events: Vec::new(),
            })?;
        let mut overlapping: Vec<ExchangeSession> = candidates
            .into_iter()
            .filter(|session| session.open_time < gap.end && session.close_time > gap.start)
            .collect();
        overlapping.sort_by_key(|session| session.session_date);
        if overlapping.is_empty() {
            return Err(UncoveredGapError {
                gap: gap.clone(),
                reason: "the gap overlaps no expected lake session".to_string(),
                healed_events: Vec::new(),
            });
        }
        let mut cursor = gap.start;
        for session in &overlapping {
            if session.open_time > cursor {
                return Err(UncoveredGapError {
                    gap: gap.clone(),
                    reason: format!(
                        "the gap extends outside expected coverage at {}",
                        session.session_date.isoformat()
                    ),
                    healed_events: Vec::new(),
                });
            }
            if session.close_time > cursor {
                cursor = session.close_time;
            }
            if cursor >= gap.end {
                return Ok(overlapping);
            }
        }
        Err(UncoveredGapError {
            gap: gap.clone(),
            reason: "the gap extends past the last expected session close".to_string(),
            healed_events: Vec::new(),
        })
    }

    fn ensure(&self, request: &DatasetRequest) -> Option<crate::lake::CoverageResponse> {
        match self.client.ensure(request) {
            Ok(EnsureResponse::Coverage(response)) => Some(response),
            Ok(EnsureResponse::Accepted(accepted)) => {
                let _ = self.client.wait_for_job(
                    &accepted.job_id,
                    self.job_timeout,
                    self.job_poll_interval,
                );
                None
            }
            Err(_) => None,
        }
    }

    fn covered_sessions(
        &self,
        gap: &CoverageGap,
        coverage: Option<&crate::lake::CoverageResponse>,
        sessions: &[ExchangeSession],
    ) -> Result<Option<Vec<ExchangeSession>>, UncoveredGapError> {
        let Some(coverage) = coverage else {
            return Ok(None);
        };
        let entries: HashMap<Date, &crate::lake::CoverageEntry> = coverage
            .entries
            .iter()
            .map(|entry| (entry.session_date, entry))
            .collect();
        let mut covered = Vec::new();
        let mut failures = Vec::new();
        for session in sessions {
            let Some(entry) = entries.get(&session.session_date) else {
                failures.push(format!(
                    "{}: ensure omitted the session",
                    session.session_date.isoformat()
                ));
                continue;
            };
            if entry.status == CoverageState::Empty {
                continue;
            }
            if !matches!(
                entry.status,
                CoverageState::Complete | CoverageState::Partial
            ) {
                failures.push(format!(
                    "{}: coverage is incomplete",
                    session.session_date.isoformat()
                ));
                continue;
            }
            if entry.object_path.is_none() {
                failures.push(format!(
                    "{}: coverage has no object path",
                    session.session_date.isoformat()
                ));
                continue;
            }
            covered.push(*session);
        }
        if !failures.is_empty() {
            return Err(UncoveredGapError {
                gap: gap.clone(),
                reason: failures.join("; "),
                healed_events: Vec::new(),
            });
        }
        Ok(Some(covered))
    }

    fn query(
        &self,
        gap: &CoverageGap,
        request: &DatasetRequest,
        covered: Option<&[ExchangeSession]>,
        sessions: &[ExchangeSession],
    ) -> Result<Vec<FileQueryFile>, UncoveredGapError> {
        let required: Vec<Date> = match covered {
            None => sessions
                .iter()
                .map(|session| session.session_date)
                .collect(),
            Some(covered) => covered.iter().map(|session| session.session_date).collect(),
        };
        if required.is_empty() {
            return Ok(Vec::new());
        }
        let response =
            self.client
                .query_files(request, false)
                .map_err(|error| UncoveredGapError {
                    gap: gap.clone(),
                    reason: error.to_string(),
                    healed_events: Vec::new(),
                })?;
        let mut files: HashMap<Date, FileQueryFile> = HashMap::new();
        for file in response.files {
            if files.insert(file.session_date, file).is_some() {
                return Err(UncoveredGapError {
                    gap: gap.clone(),
                    reason: "file query returned a duplicate lake partition".to_string(),
                    healed_events: Vec::new(),
                });
            }
        }
        let mut ordered = Vec::new();
        for session_date in &required {
            match files.get(session_date) {
                Some(file) => ordered.push(file.clone()),
                None => {
                    return Err(UncoveredGapError {
                        gap: gap.clone(),
                        reason: format!(
                            "{}: the lake returned no file for covered session",
                            session_date.isoformat()
                        ),
                        healed_events: Vec::new(),
                    })
                }
            }
        }
        Ok(ordered)
    }

    fn read(
        &self,
        gap: &CoverageGap,
        sessions: &[ExchangeSession],
        files: &[FileQueryFile],
    ) -> Result<Vec<MarketEvent>, UncoveredGapError> {
        if files.is_empty() {
            return Ok(Vec::new());
        }
        let dates: std::collections::HashSet<Date> =
            files.iter().map(|file| file.session_date).collect();
        let feed_sessions: Vec<ExchangeSession> = sessions
            .iter()
            .filter(|session| dates.contains(&session.session_date))
            .cloned()
            .collect();
        let mut feed = LakeReplayFeed::new(
            feed_sessions,
            Vec::new(),
            files.to_vec(),
            Some(Rc::clone(&self.reader)),
        )
        .map_err(|error| UncoveredGapError {
            gap: gap.clone(),
            reason: error.to_string(),
            healed_events: Vec::new(),
        })?;
        let events = feed.events().map_err(|error| UncoveredGapError {
            gap: gap.clone(),
            reason: error.to_string(),
            healed_events: Vec::new(),
        })?;
        Ok(events
            .into_iter()
            .filter(|event| gap.start < event.timestamp() && event.timestamp() < gap.end)
            .collect())
    }
}

/// Mediate the seam between a live feed and market-lake backfill.
pub struct LiveGapHealer {
    detector: LiveGapDetector,
    backfill: LakeGapBackfiller,
    stale_after: Nanos,
    clock: Option<Box<dyn Clock>>,
    actions: Vec<HealingAction>,
    emitted_through: HashMap<(String, DataType), DateTime>,
    healed_windows: HashMap<(String, DataType), Vec<(DateTime, DateTime)>>,
    duplicate_drops: u64,
    unordered_drops: u64,
}

impl LiveGapHealer {
    /// Build a healer.
    pub fn new(
        detector: LiveGapDetector,
        backfill: LakeGapBackfiller,
        stale_after: Nanos,
        clock: Option<Box<dyn Clock>>,
    ) -> Result<Self, DataError> {
        if stale_after <= 0 {
            return Err(DataError::value("stale_after must be positive"));
        }
        Ok(Self {
            detector,
            backfill,
            stale_after,
            clock,
            actions: Vec::new(),
            emitted_through: HashMap::new(),
            healed_windows: HashMap::new(),
            duplicate_drops: 0,
            unordered_drops: 0,
        })
    }

    /// The configured gap detector.
    pub fn detector(&self) -> &LiveGapDetector {
        &self.detector
    }

    /// The immutable heal-attempt trail in attempt order.
    pub fn actions(&self) -> &[HealingAction] {
        &self.actions
    }

    /// Live events dropped because they landed inside a healed interval.
    pub fn duplicate_events_dropped(&self) -> u64 {
        self.duplicate_drops
    }

    /// Events dropped because they preceded the healed timeline.
    pub fn unordered_events_dropped(&self) -> u64 {
        self.unordered_drops
    }

    /// Emit one live event, healing a revealed gap before resuming flow.
    pub fn feed(&mut self, event: MarketEvent) -> Result<Vec<MarketEvent>, UncoveredGapError> {
        let partition = event_partition(&event);
        let mut healed = Vec::new();
        let last = self.detector.last_seen(&partition.0, partition.1);
        if let Some(last) = last {
            if event.timestamp().to_nanos() - last.to_nanos() >= self.stale_after {
                let gaps = self
                    .detector
                    .detect_between(&partition.0, partition.1, last, event.timestamp())
                    .map_err(|error| UncoveredGapError {
                        gap: CoverageGap {
                            symbol: partition.0.clone(),
                            data_type: partition.1,
                            start: last,
                            end: event.timestamp(),
                        },
                        reason: error.to_string(),
                        healed_events: Vec::new(),
                    })?;
                healed = self.heal(&gaps)?;
            }
        }
        if self.should_drop(&event, &partition) {
            return Ok(healed);
        }
        self.detector.observe(&event);
        let current = self.emitted_through.get(&partition).copied();
        if current.is_none_or(|through| event.timestamp() > through) {
            self.emitted_through.insert(partition, event.timestamp());
        }
        healed.push(event);
        Ok(healed)
    }

    /// Detect and heal staleness for every tracked partition.
    pub fn tick(&mut self, now: Option<DateTime>) -> Result<Vec<MarketEvent>, UncoveredGapError> {
        let resolved = now
            .or_else(|| self.clock.as_ref().map(|clock| clock.now()))
            .ok_or_else(|| UncoveredGapError {
                gap: CoverageGap {
                    symbol: String::new(),
                    data_type: DataType::Trade,
                    start: DateTime::EPOCH,
                    end: DateTime::EPOCH,
                },
                reason: "now is required when the healer has no clock".to_string(),
                healed_events: Vec::new(),
            })?;
        let gaps = self
            .detector
            .detect(resolved, self.stale_after, None)
            .map_err(|error| UncoveredGapError {
                gap: CoverageGap {
                    symbol: String::new(),
                    data_type: DataType::Trade,
                    start: resolved,
                    end: resolved,
                },
                reason: error.to_string(),
                healed_events: Vec::new(),
            })?;
        self.heal(&gaps)
    }

    /// Backfill the given gaps in deterministic order and emit the slice.
    pub fn heal(&mut self, gaps: &[CoverageGap]) -> Result<Vec<MarketEvent>, UncoveredGapError> {
        let mut ordered = gaps.to_vec();
        ordered.sort_by(|a, b| {
            (a.symbol.as_str(), a.data_type.value(), a.start).cmp(&(
                b.symbol.as_str(),
                b.data_type.value(),
                b.start,
            ))
        });
        let mut collected = Vec::new();
        for gap in ordered {
            self.detector.mark_dirty(&gap);
            let partition = gap.partition();
            if let Some(through) = self.emitted_through.get(&partition).copied() {
                if gap.end <= through {
                    let reason = "live flow already passed the end of the gap".to_string();
                    self.actions.push(HealingAction {
                        gap: gap.clone(),
                        status: HealingStatus::Uncovered,
                        events: 0,
                        sessions: Vec::new(),
                        detail: reason.clone(),
                    });
                    return Err(UncoveredGapError {
                        gap,
                        reason,
                        healed_events: collected,
                    });
                }
            }
            let healed = match self.backfill.backfill(&gap) {
                Ok(healed) => healed,
                Err(mut error) => {
                    error.healed_events = collected;
                    self.actions.push(HealingAction {
                        gap: gap.clone(),
                        status: HealingStatus::Uncovered,
                        events: 0,
                        sessions: Vec::new(),
                        detail: error.reason.clone(),
                    });
                    return Err(error);
                }
            };
            let admitted = self.admit(&gap, healed);
            let sessions = self.overlap_dates(&gap);
            self.actions.push(HealingAction {
                gap,
                status: HealingStatus::Healed,
                events: admitted.len(),
                sessions,
                detail: format!("replayed {} lake events", admitted.len()),
            });
            collected.extend(admitted);
        }
        Ok(collected)
    }

    fn overlap_dates(&self, gap: &CoverageGap) -> Vec<Date> {
        self.detector
            .calendar()
            .sessions(gap.start.date(), gap.end.date())
            .unwrap_or_default()
            .into_iter()
            .filter(|session| session.open_time < gap.end && session.close_time > gap.start)
            .map(|session| session.session_date)
            .collect()
    }

    fn should_drop(&mut self, event: &MarketEvent, partition: &(String, DataType)) -> bool {
        let moment = event.timestamp();
        if let Some(windows) = self.healed_windows.get(partition) {
            for (window_start, window_end) in windows {
                if *window_start < moment && moment < *window_end {
                    self.duplicate_drops += 1;
                    return true;
                }
            }
        }
        if let Some(through) = self.emitted_through.get(partition).copied() {
            if moment < through {
                self.unordered_drops += 1;
                return true;
            }
        }
        false
    }

    fn admit(&mut self, gap: &CoverageGap, healed: Vec<MarketEvent>) -> Vec<MarketEvent> {
        let partition = gap.partition();
        let mut admitted = Vec::new();
        for event in healed {
            let through = self.emitted_through.get(&partition).copied();
            if through.is_some_and(|value| event.timestamp() <= value) {
                self.unordered_drops += 1;
                continue;
            }
            admitted.push(event);
        }
        if let Some(newest) = admitted.last().map(MarketEvent::timestamp) {
            let current = self.emitted_through.get(&partition).copied();
            if current.is_none_or(|value| newest > value) {
                self.emitted_through.insert(partition.clone(), newest);
            }
        }
        self.healed_windows
            .entry(partition)
            .or_default()
            .push((gap.start, gap.end));
        admitted
    }
}

#[cfg(test)]
#[path = "../tests/unit/gap_healing.rs"]
mod tests;
