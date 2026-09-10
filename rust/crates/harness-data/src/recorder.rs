//! Live market-event recording in exact market-lake schemas.
//!
//! Port of `harness.data.live_recorder`. The recorder persists incoming live
//! events below a `live/{deployment}` prefix using the published Hive partition
//! directories and the exact lake column order. Each flush atomically replaces
//! one complete part object, so a crash leaves all earlier parts readable.
//!
//! The Python recorder serializes each part with PyArrow. This port keeps the
//! deterministic row model and part layout and encodes rows with a stable,
//! self-describing binary frame instead of Parquet so the crate stays free of
//! an Arrow dependency; the row shapes and column order match the lake schema.

use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};
use std::fmt;
use std::path::{Path, PathBuf};

use harness_num::Decimal18;

use crate::clock::Clock;
use crate::error::DataError;
use crate::events::MarketEvent;
use crate::lake::{partition_path, xnys_sessions, AssetClass, DataType, ExchangeSession};
use crate::symbol::canonical_symbol;
use crate::time::{Date, DateTime, Nanos};

const LIVE_ROOT: &str = "live";
const PART_DIGITS: usize = 6;
const SESSION_WINDOW_DAYS: i32 = 10;
const SESSION_CACHE_LIMIT: usize = 8;

/// A recorded row: `(column, canonical text value)` in schema order.
pub type RecordedRow = Vec<(String, Option<String>)>;

/// Whole-object storage boundary with atomic replacement semantics.
pub trait LiveObjectStore {
    /// Atomically make `payload` the complete contents at `relative_path`.
    fn replace(&self, relative_path: &str, payload: &[u8]) -> Result<(), DataError>;
    /// Read one object.
    fn read(&self, relative_path: &str) -> Result<Vec<u8>, DataError>;
}

/// Local object store with atomic whole-file replacement.
pub struct LocalLiveObjectStore {
    root: PathBuf,
}

impl LocalLiveObjectStore {
    /// Build a store rooted at `root`, creating it if needed.
    pub fn new(root: impl AsRef<Path>) -> Result<Self, DataError> {
        let root = root
            .as_ref()
            .canonicalize()
            .unwrap_or_else(|_| root.as_ref().to_path_buf());
        std::fs::create_dir_all(&root)
            .map_err(|error| DataError::value(format!("store root: {error}")))?;
        Ok(Self { root })
    }

    fn target(&self, relative_path: &str) -> Result<PathBuf, DataError> {
        if relative_path.is_empty()
            || relative_path.contains('\\')
            || relative_path.starts_with('/')
            || relative_path
                .split('/')
                .any(|part| part.is_empty() || part == "." || part == "..")
        {
            return Err(DataError::value(
                "object path must be a non-empty relative POSIX path",
            ));
        }
        let mut target = self.root.clone();
        for part in relative_path.split('/') {
            target.push(part);
        }
        Ok(target)
    }
}

impl fmt::Debug for LocalLiveObjectStore {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("LocalLiveObjectStore")
            .field("root", &self.root)
            .finish()
    }
}

impl LiveObjectStore for LocalLiveObjectStore {
    fn replace(&self, relative_path: &str, payload: &[u8]) -> Result<(), DataError> {
        let target = self.target(relative_path)?;
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|error| DataError::value(format!("create dir: {error}")))?;
        }
        let temporary = target.with_extension("tmp");
        std::fs::write(&temporary, payload)
            .map_err(|error| DataError::value(format!("write temp part: {error}")))?;
        std::fs::rename(&temporary, &target)
            .map_err(|error| DataError::value(format!("replace part: {error}")))?;
        Ok(())
    }

    fn read(&self, relative_path: &str) -> Result<Vec<u8>, DataError> {
        std::fs::read(self.target(relative_path)?)
            .map_err(|error| DataError::value(format!("read object: {error}")))
    }
}

/// Maps one event timestamp to its lake session date.
pub trait SessionResolver {
    /// Return the lake session date that owns `moment`.
    fn session_date(&self, moment: DateTime) -> Date;
}

/// Crypto session semantics: one session per UTC calendar day.
#[derive(Clone, Copy, Debug, Default)]
pub struct UtcDaySessionResolver;

impl SessionResolver for UtcDaySessionResolver {
    fn session_date(&self, moment: DateTime) -> Date {
        moment.date()
    }
}

/// XNYS session semantics for equities and options.
#[derive(Debug, Default)]
pub struct XnysSessionResolver {
    cache: RefCell<HashMap<Date, Vec<ExchangeSession>>>,
}

impl XnysSessionResolver {
    /// Build an empty resolver.
    pub fn new() -> Self {
        Self::default()
    }

    fn sessions_for(&self, day: Date) -> Vec<ExchangeSession> {
        if let Some(cached) = self.cache.borrow().get(&day) {
            return cached.clone();
        }
        let sessions = xnys_sessions(
            day.add_days(-SESSION_WINDOW_DAYS),
            day.add_days(SESSION_WINDOW_DAYS),
        )
        .unwrap_or_default();
        let mut cache = self.cache.borrow_mut();
        if cache.len() >= SESSION_CACHE_LIMIT {
            if let Some(key) = cache.keys().next().copied() {
                cache.remove(&key);
            }
        }
        cache.insert(day, sessions.clone());
        sessions
    }
}

impl SessionResolver for XnysSessionResolver {
    fn session_date(&self, moment: DateTime) -> Date {
        let sessions = self.sessions_for(moment.date());
        let Some(previous) = sessions.first() else {
            return moment.date();
        };
        if moment < previous.open_time {
            return previous.session_date;
        }
        let mut previous = previous;
        for candidate in sessions.iter().skip(1) {
            if moment < candidate.open_time {
                if moment < previous.close_time {
                    return previous.session_date;
                }
                let midpoint = previous.close_time.to_nanos()
                    + (candidate.open_time.to_nanos() - previous.close_time.to_nanos()) / 2;
                return if moment.to_nanos() < midpoint {
                    previous.session_date
                } else {
                    candidate.session_date
                };
            }
            previous = candidate;
        }
        previous.session_date
    }
}

fn deployment_text(value: &str) -> Result<String, DataError> {
    let canonical = value.trim();
    let mut chars = canonical.chars();
    let valid = matches!(chars.next(), Some(c) if c.is_ascii_alphanumeric())
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'));
    if canonical.is_empty() || canonical == "." || canonical == ".." || !valid {
        return Err(DataError::value(
            "deployment must be one safe path segment of letters, digits, dots, dashes, or underscores",
        ));
    }
    Ok(canonical.to_string())
}

/// Build one live part path below the `live/{deployment}` prefix.
pub fn live_part_path(
    deployment: &str,
    session_date: Date,
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
    part: u64,
) -> Result<String, DataError> {
    let directory = partition_path(session_date, asset_class, data_type, symbol)?;
    let (head, filename) = directory
        .rsplit_once('/')
        .ok_or_else(|| DataError::value("partition path is missing a filename"))?;
    let stem = filename
        .strip_suffix(".parquet")
        .ok_or_else(|| DataError::value("partition path is not a parquet file"))?;
    Ok(format!(
        "{LIVE_ROOT}/{}/{head}/{stem}.part-{part:0PART_DIGITS$}.parquet",
        deployment_text(deployment)?,
        part = part,
        PART_DIGITS = PART_DIGITS
    ))
}

/// The exact lake column order for one asset class and data type.
pub fn schema_columns(asset_class: AssetClass, data_type: DataType) -> &'static [&'static str] {
    match (asset_class, data_type) {
        (AssetClass::Equity, DataType::Trade) => &[
            "timestamp",
            "symbol",
            "trade_id",
            "exchange",
            "price",
            "size",
            "conditions",
            "tape",
            "update",
        ],
        (AssetClass::Equity, DataType::Quote) => &[
            "timestamp",
            "symbol",
            "bid_exchange",
            "bid_price",
            "bid_size",
            "ask_exchange",
            "ask_price",
            "ask_size",
            "conditions",
            "tape",
        ],
        (AssetClass::Option, DataType::Trade) => &[
            "timestamp",
            "symbol",
            "exchange",
            "price",
            "size",
            "condition",
        ],
        (AssetClass::Option, DataType::Quote) => &[
            "timestamp",
            "symbol",
            "bid_exchange",
            "bid_price",
            "bid_size",
            "ask_exchange",
            "ask_price",
            "ask_size",
            "condition",
        ],
        (AssetClass::Crypto, DataType::Trade) => &[
            "timestamp",
            "symbol",
            "trade_id",
            "exchange",
            "price",
            "size",
            "taker_side",
        ],
        (AssetClass::Crypto, DataType::Quote) => &[
            "timestamp",
            "symbol",
            "bid_exchange",
            "bid_price",
            "bid_size",
            "ask_exchange",
            "ask_price",
            "ask_size",
        ],
        _ => &[],
    }
}

fn integer_size(value: Decimal18) -> Result<String, DataError> {
    let whole = value.to_i64(harness_num::RoundingMode::Truncate)?;
    let back = Decimal18::from_i64(whole);
    if back != value {
        return Err(DataError::value("size must be an integral lake size"));
    }
    if whole <= 0 || whole > u32::MAX as i64 {
        return Err(DataError::value("size exceeds the lake uint32 size range"));
    }
    Ok(whole.to_string())
}

fn integer_id(
    value: Option<&str>,
    field: &str,
    required: bool,
) -> Result<Option<String>, DataError> {
    match value {
        None => {
            if required {
                Err(DataError::value(format!(
                    "{field} is required by the lake schema"
                )))
            } else {
                Ok(None)
            }
        }
        Some(text) => {
            let parsed: i128 = text
                .parse()
                .map_err(|_| DataError::value(format!("{field} must be an integer identifier")))?;
            if parsed < 0 {
                return Err(DataError::value(format!(
                    "{field} exceeds the lake identifier range"
                )));
            }
            Ok(Some(parsed.to_string()))
        }
    }
}

/// Build the lake row for one market event.
pub fn event_row(event: &MarketEvent, asset_class: AssetClass) -> Result<RecordedRow, DataError> {
    let timestamp = Some(event.timestamp().isoformat_micros());
    let symbol = Some(event.symbol().to_string());
    match (event, asset_class) {
        (MarketEvent::Quote(quote), AssetClass::Equity | AssetClass::Option) => {
            let condition = if asset_class == AssetClass::Option {
                Some(("condition".to_string(), Some(String::new())))
            } else {
                Some(("conditions".to_string(), Some("[]".to_string())))
            };
            let mut row = vec![
                ("timestamp".to_string(), timestamp),
                ("symbol".to_string(), symbol),
                ("bid_exchange".to_string(), Some(String::new())),
                ("bid_price".to_string(), Some(quote.bid_price.to_string())),
                ("bid_size".to_string(), Some(integer_size(quote.bid_size)?)),
                ("ask_exchange".to_string(), Some(String::new())),
                ("ask_price".to_string(), Some(quote.ask_price.to_string())),
                ("ask_size".to_string(), Some(integer_size(quote.ask_size)?)),
            ];
            if let Some(condition) = condition {
                row.push(condition);
            }
            if asset_class == AssetClass::Equity {
                row.push(("tape".to_string(), Some(String::new())));
            }
            Ok(row)
        }
        (MarketEvent::Quote(quote), AssetClass::Crypto) => Ok(vec![
            ("timestamp".to_string(), timestamp),
            ("symbol".to_string(), symbol),
            ("bid_exchange".to_string(), None),
            ("bid_price".to_string(), Some(quote.bid_price.to_string())),
            ("bid_size".to_string(), Some(quote.bid_size.to_string())),
            ("ask_exchange".to_string(), None),
            ("ask_price".to_string(), Some(quote.ask_price.to_string())),
            ("ask_size".to_string(), Some(quote.ask_size.to_string())),
        ]),
        (MarketEvent::Trade(trade), AssetClass::Equity) => Ok(vec![
            ("timestamp".to_string(), timestamp),
            ("symbol".to_string(), symbol),
            (
                "trade_id".to_string(),
                integer_id(trade.trade_id.as_deref(), "trade_id", true)?,
            ),
            ("exchange".to_string(), Some(String::new())),
            ("price".to_string(), Some(trade.price.to_string())),
            ("size".to_string(), Some(integer_size(trade.quantity)?)),
            ("conditions".to_string(), Some("[]".to_string())),
            ("tape".to_string(), Some(String::new())),
            ("update".to_string(), None),
        ]),
        (MarketEvent::Trade(trade), AssetClass::Option) => Ok(vec![
            ("timestamp".to_string(), timestamp),
            ("symbol".to_string(), symbol),
            ("exchange".to_string(), Some(String::new())),
            ("price".to_string(), Some(trade.price.to_string())),
            ("size".to_string(), Some(integer_size(trade.quantity)?)),
            ("condition".to_string(), Some(String::new())),
        ]),
        (MarketEvent::Trade(trade), AssetClass::Crypto) => Ok(vec![
            ("timestamp".to_string(), timestamp),
            ("symbol".to_string(), symbol),
            (
                "trade_id".to_string(),
                integer_id(trade.trade_id.as_deref(), "trade_id", false)?,
            ),
            ("exchange".to_string(), None),
            ("price".to_string(), Some(trade.price.to_string())),
            ("size".to_string(), Some(trade.quantity.to_string())),
            ("taker_side".to_string(), None),
        ]),
        (_, AssetClass::Reference) => Err(DataError::value(
            "reference asset class is not recorded by the live recorder",
        )),
    }
}

fn encode_rows(rows: &[RecordedRow]) -> Vec<u8> {
    let mut payload = Vec::new();
    payload.extend_from_slice(b"fca-live-part-v1\0");
    payload.extend_from_slice(&(rows.len() as u64).to_be_bytes());
    for row in rows {
        payload.extend_from_slice(&(row.len() as u64).to_be_bytes());
        for (name, value) in row {
            push_bytes(&mut payload, name.as_bytes());
            match value {
                None => payload.push(0),
                Some(value) => {
                    payload.push(1);
                    push_bytes(&mut payload, value.as_bytes());
                }
            }
        }
    }
    payload
}

fn push_bytes(payload: &mut Vec<u8>, bytes: &[u8]) {
    payload.extend_from_slice(&(bytes.len() as u64).to_be_bytes());
    payload.extend_from_slice(bytes);
}

fn default_resolver(asset_class: AssetClass) -> Box<dyn SessionResolver> {
    if asset_class == AssetClass::Crypto {
        Box::new(UtcDaySessionResolver)
    } else {
        Box::new(XnysSessionResolver::new())
    }
}

/// Buffers one partition's rows and replaces one complete part per flush.
struct PartitionWriter {
    asset_class: AssetClass,
    data_type: DataType,
    deployment: String,
    part: u64,
    pending: Vec<RecordedRow>,
    rows: usize,
    session_date: Date,
    store: std::rc::Rc<dyn LiveObjectStore>,
    symbol: String,
}

impl PartitionWriter {
    fn append(&mut self, row: RecordedRow) -> Result<(), DataError> {
        self.pending.push(row);
        Ok(())
    }

    fn flush(&mut self) -> Result<(), DataError> {
        if self.pending.is_empty() {
            return Ok(());
        }
        let payload = encode_rows(&self.pending);
        let path = live_part_path(
            &self.deployment,
            self.session_date,
            self.asset_class,
            self.data_type,
            &self.symbol,
            self.part,
        )?;
        self.store.replace(&path, &payload)?;
        self.rows += self.pending.len();
        self.part += 1;
        self.pending.clear();
        Ok(())
    }
}

/// Immutable configuration for one live recording deployment.
#[derive(Clone)]
pub struct LiveRecorderConfig {
    /// Deployment path segment.
    pub deployment: String,
    /// Asset class.
    pub asset_class: AssetClass,
    /// Whole-object store.
    pub store: std::rc::Rc<dyn LiveObjectStore>,
    /// Engine clock.
    pub clock: std::rc::Rc<dyn Clock>,
    /// Rows per part flush.
    pub batch_size: usize,
    /// Flush interval in nanoseconds, if any.
    pub flush_interval: Option<Nanos>,
    /// Session resolver override.
    pub session_resolver: Option<std::rc::Rc<dyn SessionResolver>>,
}

impl fmt::Debug for LiveRecorderConfig {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("LiveRecorderConfig")
            .field("deployment", &self.deployment)
            .field("asset_class", &self.asset_class)
            .field("batch_size", &self.batch_size)
            .finish()
    }
}

impl LiveRecorderConfig {
    /// Build a validated configuration.
    pub fn new(
        deployment: &str,
        asset_class: AssetClass,
        store: std::rc::Rc<dyn LiveObjectStore>,
        clock: std::rc::Rc<dyn Clock>,
        batch_size: usize,
    ) -> Result<Self, DataError> {
        if batch_size == 0 {
            return Err(DataError::value("batch_size must be a positive integer"));
        }
        Ok(Self {
            deployment: deployment_text(deployment)?,
            asset_class,
            store,
            clock,
            batch_size,
            flush_interval: None,
            session_resolver: None,
        })
    }
}

/// Persist live market events as lake-schema part files.
pub struct LiveRecorder {
    asset_class: AssetClass,
    closed: bool,
    config: LiveRecorderConfig,
    finalized_rows: BTreeMap<(DataType, String), usize>,
    resolver: Box<dyn SessionResolver>,
    sessions: BTreeMap<(DataType, String), Date>,
    writers: BTreeMap<(DataType, String), PartitionWriter>,
}

impl LiveRecorder {
    /// Build a recorder from `config`.
    pub fn new(config: LiveRecorderConfig) -> Self {
        let resolver: Box<dyn SessionResolver> = match &config.session_resolver {
            Some(resolver) => Box::new(ResolverHandle(std::rc::Rc::clone(resolver))),
            None => default_resolver(config.asset_class),
        };
        Self {
            asset_class: config.asset_class,
            closed: false,
            config,
            finalized_rows: BTreeMap::new(),
            resolver,
            sessions: BTreeMap::new(),
            writers: BTreeMap::new(),
        }
    }

    /// The recorder configuration.
    pub fn config(&self) -> &LiveRecorderConfig {
        &self.config
    }

    /// Whether the recording has been finalized.
    pub fn closed(&self) -> bool {
        self.closed
    }

    /// Total rows buffered across active partitions.
    pub fn pending_rows(&self) -> usize {
        self.writers
            .values()
            .map(|writer| writer.pending.len())
            .sum()
    }

    /// Rows written to part files per `data_type/symbol` key.
    pub fn row_counts(&self) -> BTreeMap<String, usize> {
        let mut counts: BTreeMap<String, usize> = self
            .finalized_rows
            .iter()
            .map(|(key, rows)| (format!("{}/{}", key.0.value(), key.1), *rows))
            .collect();
        for (partition, writer) in &self.writers {
            let name = format!("{}/{}", partition.0.value(), partition.1);
            counts.insert(name, writer.rows);
        }
        counts
    }

    /// Persist one incoming live market event.
    pub fn record(&mut self, event: &MarketEvent) -> Result<(), DataError> {
        if self.closed {
            return Err(DataError::value("the live recorder is closed"));
        }
        let data_type = match event.event_type() {
            crate::events::EventType::Quote => DataType::Quote,
            crate::events::EventType::Trade => DataType::Trade,
            _ => return Err(DataError::value("event must be a QuoteEvent or TradeEvent")),
        };
        let symbol = canonical_symbol(event.symbol())?;
        let session = self.resolver.session_date(event.timestamp());
        let partition = (data_type, symbol.clone());
        if let Some(active) = self.sessions.get(&partition).copied() {
            if session < active {
                return Err(DataError::value(format!(
                    "event session {} precedes the active session {} for {}/{}",
                    session.isoformat(),
                    active.isoformat(),
                    data_type.value(),
                    symbol
                )));
            }
        }
        let rotate = self
            .sessions
            .get(&partition)
            .is_none_or(|active| session > *active);
        if rotate {
            self.rotate(&partition, session);
        }
        let row = event_row(event, self.asset_class)?;
        if let Some(writer) = self.writers.get_mut(&partition) {
            if writer.pending.len() >= self.config.batch_size {
                writer.flush()?;
            }
            writer.append(row)?;
        }
        Ok(())
    }

    /// Replace one complete part per partition carrying pending rows.
    pub fn flush(&mut self) -> Result<(), DataError> {
        if self.closed {
            return Err(DataError::value("the live recorder is closed"));
        }
        for writer in self.writers.values_mut() {
            writer.flush()?;
        }
        Ok(())
    }

    /// Flush every pending batch so shutdown leaves every file readable.
    pub fn close(&mut self) -> Result<(), DataError> {
        if self.closed {
            return Ok(());
        }
        let partitions: Vec<(DataType, String)> = self.writers.keys().cloned().collect();
        for partition in partitions {
            if let Some(writer) = self.writers.get_mut(&partition) {
                writer.flush()?;
                let rows = writer.rows;
                self.finalized_rows.insert(partition, rows);
            }
        }
        self.closed = true;
        Ok(())
    }

    fn rotate(&mut self, partition: &(DataType, String), session: Date) {
        if let Some(mut writer) = self.writers.remove(partition) {
            let _ = writer.flush();
            *self.finalized_rows.entry(partition.clone()).or_insert(0) += writer.rows;
        }
        self.sessions.insert(partition.clone(), session);
        self.writers.insert(
            partition.clone(),
            PartitionWriter {
                asset_class: self.asset_class,
                data_type: partition.0,
                deployment: self.config.deployment.clone(),
                part: 0,
                pending: Vec::new(),
                rows: 0,
                session_date: session,
                store: std::rc::Rc::clone(&self.config.store),
                symbol: partition.1.clone(),
            },
        );
    }
}

struct ResolverHandle(std::rc::Rc<dyn SessionResolver>);

impl SessionResolver for ResolverHandle {
    fn session_date(&self, moment: DateTime) -> Date {
        self.0.session_date(moment)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clock::ManualClock;
    use crate::events::TradeEvent;
    use std::str::FromStr;

    #[test]
    fn live_part_path_matches_layout() {
        let date = Date::from_ymd(2024, 1, 5).unwrap();
        assert_eq!(
            live_part_path("prod", date, AssetClass::Equity, DataType::Trade, "AAPL", 3).unwrap(),
            "live/prod/date=2024-01-05/asset_class=equity/data_type=trade/AAPL.part-000003.parquet"
        );
    }

    #[test]
    fn recorder_writes_rows_in_schema_order() {
        let store = std::rc::Rc::new(
            LocalLiveObjectStore::new(std::env::temp_dir().join("fca-live-recorder-test")).unwrap(),
        );
        let clock: std::rc::Rc<dyn Clock> =
            std::rc::Rc::new(ManualClock::new(DateTime::from_micros(0)));
        let config =
            LiveRecorderConfig::new("prod", AssetClass::Equity, store.clone(), clock, 2).unwrap();
        let mut recorder = LiveRecorder::new(config);
        let mut event = TradeEvent::new(
            "AAPL".to_string(),
            DateTime::from_ymd_hms(2024, 1, 5, 15, 30, 0, 0).unwrap(),
            1,
            Decimal18::from_str("10.5").unwrap(),
            Decimal18::from_str("100").unwrap(),
        );
        event.trade_id = Some("1".to_string());
        recorder.record(&MarketEvent::Trade(event)).unwrap();
        recorder.close().unwrap();
        assert!(recorder.closed());
        assert_eq!(recorder.row_counts().get("trade/AAPL").copied(), Some(1));
        let payload = store
            .read("live/prod/date=2024-01-05/asset_class=equity/data_type=trade/AAPL.part-000000.parquet")
            .unwrap();
        assert!(payload.starts_with(b"fca-live-part-v1\0"));
    }
}
