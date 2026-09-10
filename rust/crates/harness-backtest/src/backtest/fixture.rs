//! Session windows, synthetic benchmark data, and replay fixtures.
//!
//! Port of `harness.runtime.backtest.fixture`. The synthetic benchmark is
//! byte-for-byte reproducible: sessions are deterministic UTC weekday windows,
//! and the price path is drawn from the CPython-compatible
//! [`PythonRandom`](super::rng::PythonRandom) seeded by the run.

use std::collections::BTreeSet;

use harness_num::Decimal18;

use crate::canonical_json::JsonValue;
use crate::sha256::{digest_hex, Sha256};

use super::config::BacktestConfig;
use super::errors::{BacktestError, ONE_DAY_NS};
use super::rng::PythonRandom;

/// Exact knobs for the deterministic local benchmark data fixture.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SyntheticDataOptions {
    /// Quoted symbol.
    pub symbol: String,
    /// Quotes generated per session.
    pub quotes_per_session: u32,
    /// Trades generated per session.
    pub trades_per_session: u32,
    /// Session-open reference price.
    pub base_price: Decimal18,
    /// Minimum price tick.
    pub tick: Decimal18,
    /// Maximum absolute drift in ticks per quote.
    pub drift_ticks: i64,
    /// Half-spread in ticks.
    pub spread_ticks: i64,
    /// Quote size.
    pub quote_quantity: Decimal18,
    /// Trade size.
    pub trade_quantity: Decimal18,
    /// Session open as `(hour, minute)` UTC.
    pub session_open: (u32, u32),
    /// Session close as `(hour, minute)` UTC.
    pub session_close: (u32, u32),
    /// Reporting venue.
    pub venue: String,
}

impl Default for SyntheticDataOptions {
    fn default() -> Self {
        Self {
            symbol: "SPY".to_string(),
            quotes_per_session: 12,
            trades_per_session: 6,
            base_price: Decimal18::from_unscaled(450_000_000_000_000_000_000).expect("450"),
            tick: Decimal18::from_unscaled(10_000_000_000_000_000).expect("0.01"),
            drift_ticks: 4,
            spread_ticks: 1,
            quote_quantity: Decimal18::from_unscaled(100_000_000_000_000_000_000).expect("100"),
            trade_quantity: Decimal18::from_unscaled(100_000_000_000_000_000_000).expect("100"),
            session_open: (13, 30),
            session_close: (20, 0),
            venue: "XNYS".to_string(),
        }
    }
}

impl SyntheticDataOptions {
    /// Validate the option bounds, mirroring the Python `__post_init__`.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] for any out-of-range value.
    pub fn validate(&self) -> Result<(), BacktestError> {
        if self.quotes_per_session < 1 {
            return Err(BacktestError::Configuration(
                "quotes_per_session must be positive".to_string(),
            ));
        }
        if self.trades_per_session > self.quotes_per_session {
            return Err(BacktestError::Configuration(
                "trades_per_session must not exceed quotes_per_session".to_string(),
            ));
        }
        if !self.base_price.is_positive() || !self.tick.is_positive() {
            return Err(BacktestError::Configuration(
                "base_price and tick must be positive".to_string(),
            ));
        }
        if self.drift_ticks < 0 || self.spread_ticks < 1 {
            return Err(BacktestError::Configuration(
                "drift_ticks and spread_ticks must be non-negative".to_string(),
            ));
        }
        if !self.quote_quantity.is_positive() || !self.trade_quantity.is_positive() {
            return Err(BacktestError::Configuration(
                "quantities must be positive".to_string(),
            ));
        }
        for (name, value) in [
            ("session_open", self.session_open),
            ("session_close", self.session_close),
        ] {
            if value.0 > 23 || value.1 > 59 {
                return Err(BacktestError::Configuration(format!(
                    "{name} must be a (hour, minute) pair"
                )));
            }
        }
        Ok(())
    }
}

/// One deterministic UTC session window.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExchangeSession {
    /// Days since the Unix epoch for the session date.
    pub session_date_days: i64,
    /// Window open in epoch nanoseconds.
    pub open_ns: i64,
    /// Window close in epoch nanoseconds.
    pub close_ns: i64,
}

impl ExchangeSession {
    /// The session date as `YYYY-MM-DD`.
    #[must_use]
    pub fn date_string(&self) -> String {
        date_string(self.session_date_days)
    }
}

/// One deterministic market event.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MarketEvent {
    /// A top-of-book quote.
    Quote {
        /// Event identifier.
        event_id: String,
        /// Event time in epoch nanoseconds.
        timestamp_ns: i64,
        /// Global sequence.
        sequence: u64,
        /// Instrument symbol.
        symbol: String,
        /// Best bid.
        bid: Decimal18,
        /// Best ask.
        ask: Decimal18,
        /// Bid size.
        bid_quantity: Decimal18,
        /// Ask size.
        ask_quantity: Decimal18,
        /// Reporting venue.
        venue: String,
    },
    /// A trade print.
    Trade {
        /// Event identifier.
        event_id: String,
        /// Event time in epoch nanoseconds.
        timestamp_ns: i64,
        /// Global sequence.
        sequence: u64,
        /// Instrument symbol.
        symbol: String,
        /// Print price.
        price: Decimal18,
        /// Print quantity.
        quantity: Decimal18,
        /// Reporting venue.
        venue: String,
        /// Trade identifier.
        trade_id: String,
    },
}

impl MarketEvent {
    /// Event time in epoch nanoseconds.
    #[must_use]
    pub const fn timestamp_ns(&self) -> i64 {
        match self {
            Self::Quote { timestamp_ns, .. } | Self::Trade { timestamp_ns, .. } => *timestamp_ns,
        }
    }

    /// The instrument symbol.
    #[must_use]
    pub fn symbol(&self) -> &str {
        match self {
            Self::Quote { symbol, .. } | Self::Trade { symbol, .. } => symbol,
        }
    }

    /// Canonical JSON for fingerprinting.
    fn to_json(&self) -> JsonValue {
        match self {
            Self::Quote {
                event_id,
                timestamp_ns,
                sequence,
                symbol,
                bid,
                ask,
                bid_quantity,
                ask_quantity,
                venue,
            } => JsonValue::object([
                ("kind", JsonValue::string("quote")),
                ("event_id", JsonValue::String(event_id.clone())),
                ("timestamp_ns", JsonValue::Int(*timestamp_ns)),
                ("sequence", JsonValue::UInt(*sequence)),
                ("symbol", JsonValue::String(symbol.clone())),
                ("bid", JsonValue::String(bid.to_string())),
                ("ask", JsonValue::String(ask.to_string())),
                ("bid_quantity", JsonValue::String(bid_quantity.to_string())),
                ("ask_quantity", JsonValue::String(ask_quantity.to_string())),
                ("venue", JsonValue::String(venue.clone())),
            ]),
            Self::Trade {
                event_id,
                timestamp_ns,
                sequence,
                symbol,
                price,
                quantity,
                venue,
                trade_id,
            } => JsonValue::object([
                ("kind", JsonValue::string("trade")),
                ("event_id", JsonValue::String(event_id.clone())),
                ("timestamp_ns", JsonValue::Int(*timestamp_ns)),
                ("sequence", JsonValue::UInt(*sequence)),
                ("symbol", JsonValue::String(symbol.clone())),
                ("price", JsonValue::String(price.to_string())),
                ("quantity", JsonValue::String(quantity.to_string())),
                ("venue", JsonValue::String(venue.clone())),
                ("trade_id", JsonValue::String(trade_id.clone())),
            ]),
        }
    }
}

/// Deterministic replay fingerprints.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReplayFingerprints {
    /// Fingerprint of the addressed files.
    pub file: String,
    /// Fingerprint of the replayed rows.
    pub row: String,
    /// Domain-separated fingerprint over file and rows.
    pub data: String,
}

impl ReplayFingerprints {
    /// Fingerprint an in-memory event stream with no addressed files.
    #[must_use]
    pub fn of_events(events: &[MarketEvent]) -> Self {
        let file = {
            let mut hasher = Sha256::new();
            hasher.update(b"fca:harness-replay-files:v1\0");
            hasher.finalize_hex()
        };
        let row = {
            let mut hasher = Sha256::new();
            hasher.update(b"fca:harness-replay-rows:v1\0");
            for event in events {
                hasher.update(event.to_json().to_canonical_string().as_bytes());
                hasher.update(b"\n");
            }
            hasher.finalize_hex()
        };
        let data = {
            let mut payload = String::from("fca:harness-replay-data:v1\0");
            payload.push_str(&file);
            payload.push('\0');
            payload.push_str(&row);
            digest_hex(payload.as_bytes())
        };
        Self { file, row, data }
    }
}

/// One deterministic, re-iterable replay source for a backtest.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReplayFixture {
    /// Session windows.
    pub sessions: Vec<ExchangeSession>,
    /// Materialized events in replay order.
    pub events: Vec<MarketEvent>,
    /// Fixture source identifier.
    pub source_id: String,
    /// Instrument symbols.
    pub symbols: Vec<String>,
    /// Fingerprints.
    pub fingerprints: ReplayFingerprints,
    /// Optional expected data fingerprint to validate.
    pub expected_data_fingerprint: Option<String>,
}

impl ReplayFixture {
    /// Number of complete files addressed by this fixture.
    #[must_use]
    pub const fn file_count(&self) -> usize {
        0
    }

    /// Replay the fixture once and return its events and fingerprints.
    #[must_use]
    pub fn materialize(&self) -> (&[MarketEvent], &ReplayFingerprints) {
        (&self.events, &self.fingerprints)
    }
}

/// Runnability assurance evidence for one local market-data fixture.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DataPreflight {
    /// Fixture source identifier.
    pub source_id: String,
    /// Number of sessions.
    pub session_count: usize,
    /// Number of replayed events.
    pub event_count: usize,
    /// Number of quote events.
    pub quote_count: usize,
    /// Number of trade events.
    pub trade_count: usize,
    /// Number of addressed files.
    pub file_count: usize,
    /// Instrument symbols.
    pub symbols: Vec<String>,
    /// Replay fingerprints.
    pub fingerprints: ReplayFingerprints,
}

impl DataPreflight {
    /// Return a canonical, JSON-ready representation.
    #[must_use]
    pub fn to_json(&self) -> JsonValue {
        JsonValue::object([
            ("source_id", JsonValue::String(self.source_id.clone())),
            ("session_count", JsonValue::UInt(self.session_count as u64)),
            ("event_count", JsonValue::UInt(self.event_count as u64)),
            ("quote_count", JsonValue::UInt(self.quote_count as u64)),
            ("trade_count", JsonValue::UInt(self.trade_count as u64)),
            ("file_count", JsonValue::UInt(self.file_count as u64)),
            (
                "symbols",
                JsonValue::Array(
                    self.symbols
                        .iter()
                        .map(|symbol| JsonValue::String(symbol.clone()))
                        .collect(),
                ),
            ),
            (
                "replay_file_fingerprint",
                JsonValue::String(self.fingerprints.file.clone()),
            ),
            (
                "replay_row_fingerprint",
                JsonValue::String(self.fingerprints.row.clone()),
            ),
            (
                "replay_data_fingerprint",
                JsonValue::String(self.fingerprints.data.clone()),
            ),
        ])
    }
}

/// Return deterministic UTC weekday sessions inside the run window.
#[must_use]
pub fn backtest_sessions(
    configuration: &BacktestConfig,
    options: &SyntheticDataOptions,
) -> Vec<ExchangeSession> {
    let mut sessions = Vec::new();
    let first_day = configuration.start_ns.div_euclid(ONE_DAY_NS);
    let last_day = configuration.end_ns.div_euclid(ONE_DAY_NS);
    for day in first_day..=last_day {
        if weekday(day) < 5 {
            if let Some(session) = session_window(configuration, day, options) {
                sessions.push(session);
            }
        }
    }
    sessions
}

fn session_window(
    configuration: &BacktestConfig,
    day: i64,
    options: &SyntheticDataOptions,
) -> Option<ExchangeSession> {
    let day_start = day * ONE_DAY_NS;
    let open_ns = day_start
        + i64::from(options.session_open.0) * 3_600_000_000_000
        + i64::from(options.session_open.1) * 60_000_000_000;
    let close_ns = day_start
        + i64::from(options.session_close.0) * 3_600_000_000_000
        + i64::from(options.session_close.1) * 60_000_000_000;
    let start = open_ns.max(configuration.start_ns);
    let end = close_ns.min(configuration.end_ns);
    if end <= start {
        return None;
    }
    Some(ExchangeSession {
        session_date_days: day,
        open_ns: start,
        close_ns: end,
    })
}

/// Return an in-memory deterministic benchmark fixture for a run.
///
/// # Errors
///
/// Returns [`BacktestError`] when the options are invalid.
pub fn synthetic_fixture(
    configuration: &BacktestConfig,
    options: &SyntheticDataOptions,
) -> Result<ReplayFixture, BacktestError> {
    options.validate()?;
    let symbol = configuration
        .symbols
        .first()
        .cloned()
        .unwrap_or_else(|| options.symbol.clone())
        .to_uppercase();
    let sessions = backtest_sessions(configuration, options);
    let events = synthetic_events(configuration, options, &symbol, &sessions)?;
    let fingerprints = ReplayFingerprints::of_events(&events);
    Ok(ReplayFixture {
        sessions,
        events,
        source_id: "synthetic".to_string(),
        symbols: vec![symbol],
        fingerprints,
        expected_data_fingerprint: None,
    })
}

fn synthetic_events(
    configuration: &BacktestConfig,
    options: &SyntheticDataOptions,
    symbol: &str,
    sessions: &[ExchangeSession],
) -> Result<Vec<MarketEvent>, BacktestError> {
    let mut rng = PythonRandom::from_seed(configuration.seed);
    let two = Decimal18::from_i64(2);
    let hundred = Decimal18::from_i64(100);
    let half_spread = options
        .tick
        .checked_mul(Decimal18::from_i64(options.spread_ticks))
        .and_then(|value| value.checked_div(two))
        .map_err(|error| BacktestError::Configuration(error.to_string()))?;
    let floor = options
        .tick
        .checked_mul(hundred)
        .map_err(|error| BacktestError::Configuration(error.to_string()))?;
    let mut quotes = Vec::new();
    let mut trades = Vec::new();
    let mut quote_sequence = 0u64;
    let mut trade_sequence = 0u64;
    let mut mid = options.base_price;
    for session in sessions {
        let steps = options.quotes_per_session;
        let span_us = (session.close_ns - session.open_ns).div_euclid(1_000);
        #[allow(clippy::cast_possible_wrap)]
        let step_us = span_us / i64::from(steps);
        let mut stamps = Vec::new();
        let mut mids = Vec::new();
        for index in 0..steps {
            let stamp = session.open_ns + step_us * i64::from(index) * 1_000;
            let drift = rng.randint(-options.drift_ticks, options.drift_ticks);
            mid = mid
                .checked_add(
                    options
                        .tick
                        .checked_mul(Decimal18::from_i64(drift))
                        .map_err(|error| BacktestError::Configuration(error.to_string()))?,
                )
                .map_err(|error| BacktestError::Configuration(error.to_string()))?;
            if mid.less_than(floor) {
                mid = floor;
            }
            let bid = mid
                .checked_sub(half_spread)
                .map_err(|error| BacktestError::Configuration(error.to_string()))?;
            let ask = mid
                .checked_add(half_spread)
                .map_err(|error| BacktestError::Configuration(error.to_string()))?;
            quotes.push(MarketEvent::Quote {
                event_id: format!("{symbol}-q-{}-{index}", session.date_string()),
                timestamp_ns: stamp,
                sequence: quote_sequence,
                symbol: symbol.to_string(),
                bid,
                ask,
                bid_quantity: options.quote_quantity,
                ask_quantity: options.quote_quantity,
                venue: options.venue.clone(),
            });
            quote_sequence += 1;
            stamps.push(stamp);
            mids.push(mid);
        }
        if options.trades_per_session > 0 {
            for index in 0..options.trades_per_session {
                let position =
                    (((index + 1) * steps) / (options.trades_per_session + 1)).min(steps - 1);
                let position = position as usize;
                trades.push(MarketEvent::Trade {
                    event_id: format!("{symbol}-t-{}-{index}", session.date_string()),
                    timestamp_ns: stamps[position] + (step_us / 2) * 1_000,
                    sequence: trade_sequence,
                    symbol: symbol.to_string(),
                    price: mids[position],
                    quantity: options.trade_quantity,
                    venue: options.venue.clone(),
                    trade_id: format!("{}-{index}", session.date_string()),
                });
                trade_sequence += 1;
            }
        }
    }
    let mut events: Vec<MarketEvent> = quotes;
    events.extend(trades);
    Ok(events)
}

/// Days since the Unix epoch for a proleptic Gregorian date.
#[cfg(test)]
pub(crate) fn days_from_civil(mut year: i64, month: i64, day: i64) -> i64 {
    if month <= 2 {
        year -= 1;
    }
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let yoe = year - era * 400;
    let adjusted_month = if month > 2 { month - 3 } else { month + 9 };
    let doy = (153 * adjusted_month + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Weekday for a day count since the epoch (Monday = 0).
fn weekday(days: i64) -> i64 {
    (days + 3).rem_euclid(7)
}

/// Format a day count since the epoch as `YYYY-MM-DD`.
#[must_use]
pub fn date_string(days: i64) -> String {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { year + 1 } else { year };
    format!("{year:04}-{month:02}-{day:02}")
}

/// Return the sorted set of symbols carried by a fixture.
#[must_use]
pub fn fixture_symbols(fixture: &ReplayFixture) -> Vec<String> {
    let set: BTreeSet<String> = fixture
        .events
        .iter()
        .map(|event| event.symbol().to_string())
        .collect();
    set.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> BacktestConfig {
        // 2024-01-01T00:00:00Z .. 2024-01-03T00:00:00Z (Mon/Tue sessions).
        let start = days_from_civil(2024, 1, 1) * ONE_DAY_NS;
        let end = days_from_civil(2024, 1, 3) * ONE_DAY_NS;
        BacktestConfig::new("run", start, end, 7).expect("config")
    }

    #[test]
    fn weekday_matches_python() {
        assert_eq!(weekday(days_from_civil(2024, 1, 1)), 0); // Monday
        assert_eq!(weekday(days_from_civil(2024, 1, 2)), 1); // Tuesday
        assert_eq!(weekday(days_from_civil(2024, 1, 6)), 5); // Saturday
        assert_eq!(date_string(days_from_civil(2024, 1, 2)), "2024-01-02");
    }

    #[test]
    fn synthetic_events_match_python_drift() {
        let fixture =
            synthetic_fixture(&config(), &SyntheticDataOptions::default()).expect("fixture");
        // Two weekday sessions, 12 quotes and 6 trades each.
        assert_eq!(fixture.events.len(), 36);
        let quotes: Vec<&MarketEvent> = fixture
            .events
            .iter()
            .filter(|event| matches!(event, MarketEvent::Quote { .. }))
            .collect();
        assert_eq!(quotes.len(), 24);
        // First mid: 450.00 + 1 * 0.01 = 450.01, bid 450.005.
        let MarketEvent::Quote { bid, .. } = quotes[0] else {
            unreachable!()
        };
        assert_eq!(bid.to_string(), "450.005000000000000000");
    }

    #[test]
    fn synthetic_fixture_is_deterministic() {
        let left = synthetic_fixture(&config(), &SyntheticDataOptions::default()).expect("left");
        let right = synthetic_fixture(&config(), &SyntheticDataOptions::default()).expect("right");
        assert_eq!(left, right);
        assert_eq!(left.fingerprints, right.fingerprints);
    }
}
