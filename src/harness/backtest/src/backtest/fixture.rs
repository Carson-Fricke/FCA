//! Session windows, synthetic benchmark data, and replay fixtures.
//!
//! Port of `harness.runtime.backtest.fixture`. The synthetic benchmark is
//! byte-for-byte reproducible: sessions are deterministic UTC weekday windows,
//! and the price path is drawn from the CPython-compatible
//! [`PythonRandom`](super::rng::PythonRandom) seeded by the run.

use std::collections::BTreeSet;

use num::{Decimal18, RoundingMode, SCALE};

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
    /// Fractional digit count carried by `base_price` in the Python reference.
    ///
    /// Python `Decimal` values remember the scale of their literals and of each
    /// arithmetic result, so `str(Decimal("450.00"))` is `"450.00"`, not
    /// `"450"`. The scale-18 Rust port cannot recover that scale from the value
    /// alone, so the canonical benchmark records it here for the replay
    /// fingerprint, which must reproduce the Python `str(Decimal)` bytes.
    pub base_price_scale: u32,
    /// Fractional digit count carried by `tick` in the Python reference.
    pub tick_scale: u32,
    /// Fractional digit count carried by the quote and trade quantities.
    pub quantity_scale: u32,
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
            base_price_scale: 2,
            tick_scale: 2,
            quantity_scale: 0,
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
    /// One OHLCV bar.
    ///
    /// Bars may coexist with ticks. Their interval comes from the validated
    /// requirement plan; incompatible multi-interval plans are rejected.
    Bar {
        /// Event identifier.
        event_id: String,
        /// Event time in epoch nanoseconds.
        timestamp_ns: i64,
        /// Global sequence.
        sequence: u64,
        /// Instrument symbol.
        symbol: String,
        /// Interval open.
        open: Decimal18,
        /// Interval high.
        high: Decimal18,
        /// Interval low.
        low: Decimal18,
        /// Interval close.
        close: Decimal18,
        /// Interval volume.
        volume: Decimal18,
    },
}

impl MarketEvent {
    /// Event time in epoch nanoseconds.
    #[must_use]
    pub const fn timestamp_ns(&self) -> i64 {
        match self {
            Self::Quote { timestamp_ns, .. }
            | Self::Trade { timestamp_ns, .. }
            | Self::Bar { timestamp_ns, .. } => *timestamp_ns,
        }
    }

    /// The instrument symbol.
    #[must_use]
    pub fn symbol(&self) -> &str {
        match self {
            Self::Quote { symbol, .. } | Self::Trade { symbol, .. } | Self::Bar { symbol, .. } => {
                symbol
            }
        }
    }

    /// The event's source-local sequence number.
    #[must_use]
    pub const fn sequence(&self) -> u64 {
        match self {
            Self::Quote { sequence, .. }
            | Self::Trade { sequence, .. }
            | Self::Bar { sequence, .. } => *sequence,
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
            Self::Bar {
                event_id,
                timestamp_ns,
                sequence,
                symbol,
                open,
                high,
                low,
                close,
                volume,
            } => JsonValue::object([
                ("kind", JsonValue::string("bar")),
                ("event_id", JsonValue::String(event_id.clone())),
                ("timestamp_ns", JsonValue::Int(*timestamp_ns)),
                ("sequence", JsonValue::UInt(*sequence)),
                ("symbol", JsonValue::String(symbol.clone())),
                ("open", JsonValue::String(open.to_string())),
                ("high", JsonValue::String(high.to_string())),
                ("low", JsonValue::String(low.to_string())),
                ("close", JsonValue::String(close.to_string())),
                ("volume", JsonValue::String(volume.to_string())),
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

/// `10^exponent` as `u128`; `exponent` is at most [`SCALE`].
fn pow10(exponent: u32) -> u128 {
    let mut result = 1u128;
    let mut step = 0u32;
    while step < exponent {
        result *= 10;
        step += 1;
    }
    result
}

/// The fewest fractional digits that represent `value` exactly.
fn minimal_scale(value: Decimal18) -> u32 {
    if value.is_zero() {
        return 0;
    }
    let magnitude = value.unscaled().unsigned_abs();
    for scale in 0..=SCALE {
        if magnitude.is_multiple_of(pow10(SCALE - scale)) {
            return scale;
        }
    }
    SCALE
}

/// Render `value` with exactly `scale` fractional digits, rounding half-even.
///
/// Mirrors Python `str(Decimal)`: the arithmetic scale is explicit, so
/// `450.00` renders as `"450.00"` rather than the scale-18 `"450.000000000000000000"`.
pub(crate) fn python_decimal(value: Decimal18, scale: u32) -> String {
    let rounded = value
        .round_to(RoundingMode::HalfEven, scale)
        .unwrap_or(value);
    let negative = rounded.is_negative();
    let magnitude = rounded.unscaled().unsigned_abs();
    // `units` is the value scaled up by `10^scale`.
    let units = magnitude / pow10(SCALE - scale);
    let whole = units / pow10(scale);
    let fraction = units % pow10(scale);
    let mut out = String::new();
    if negative {
        out.push('-');
    }
    out.push_str(&whole.to_string());
    if scale > 0 {
        out.push('.');
        let digits = fraction.to_string();
        for _ in 0..(scale as usize - digits.len()) {
            out.push('0');
        }
        out.push_str(&digits);
    }
    out
}

/// Framed UTF-8 fields, byte-identical to `harness.data.replay._canonical_fields`.
fn canonical_fields(values: &[String]) -> Vec<u8> {
    let mut output = Vec::new();
    for value in values {
        let payload = value.as_bytes();
        output.extend_from_slice(&(payload.len() as u64).to_be_bytes());
        output.extend_from_slice(payload);
    }
    output
}

/// One Python `_update_frame`: a big-endian `u64` length then the payload.
fn update_frame(hasher: &mut Sha256, payload: &[u8]) {
    hasher.update(&(payload.len() as u64).to_be_bytes());
    hasher.update(payload);
}

/// Format an epoch nanosecond instant as Python `isoformat(timespec="microseconds")`.
fn timestamp_isoformat_micros(timestamp_ns: i64) -> String {
    let mut text = crate::results::recorder::format_timestamp_ns(timestamp_ns);
    if text.ends_with('Z') {
        text.pop();
        text.push_str("+00:00");
    }
    text
}

/// One replay event frame, byte-identical to `harness.data.replay`.
fn event_framed_fields(
    event: &MarketEvent,
    options: &SyntheticDataOptions,
    quote_scale: u32,
    trade_scale: u32,
) -> Vec<u8> {
    match event {
        MarketEvent::Quote {
            event_id,
            timestamp_ns,
            sequence,
            symbol,
            bid,
            ask,
            bid_quantity,
            ask_quantity,
            venue,
        } => canonical_fields(&[
            "quote".to_string(),
            event_id.clone(),
            timestamp_isoformat_micros(*timestamp_ns),
            sequence.to_string(),
            symbol.clone(),
            python_decimal(*bid, quote_scale),
            python_decimal(*bid_quantity, options.quantity_scale),
            python_decimal(*ask, quote_scale),
            python_decimal(*ask_quantity, options.quantity_scale),
            venue.clone(),
        ]),
        MarketEvent::Trade {
            event_id,
            timestamp_ns,
            sequence,
            symbol,
            price,
            quantity,
            venue,
            trade_id,
        } => canonical_fields(&[
            "trade".to_string(),
            event_id.clone(),
            timestamp_isoformat_micros(*timestamp_ns),
            sequence.to_string(),
            symbol.clone(),
            python_decimal(*price, trade_scale),
            python_decimal(*quantity, options.quantity_scale),
            venue.clone(),
            trade_id.clone(),
        ]),
        MarketEvent::Bar {
            event_id,
            timestamp_ns,
            sequence,
            symbol,
            open,
            high,
            low,
            close,
            volume,
        } => canonical_fields(&[
            "bar".to_string(),
            event_id.clone(),
            timestamp_isoformat_micros(*timestamp_ns),
            sequence.to_string(),
            symbol.clone(),
            python_decimal(*open, trade_scale),
            python_decimal(*high, trade_scale),
            python_decimal(*low, trade_scale),
            python_decimal(*close, trade_scale),
            python_decimal(*volume, options.quantity_scale),
        ]),
    }
}

/// The Python arithmetic scales used by the synthetic quote and trade values.
#[must_use]
pub(crate) fn synthetic_scales(options: &SyntheticDataOptions) -> (u32, u32) {
    let mid_scale = options.base_price_scale.max(options.tick_scale);
    let half_spread = options
        .tick
        .checked_mul(Decimal18::from_i64(options.spread_ticks))
        .and_then(|value| value.checked_div(Decimal18::from_i64(2)))
        .unwrap_or(Decimal18::ZERO);
    (mid_scale.max(minimal_scale(half_spread)), mid_scale)
}

/// Compute the Python `harness.data.replay` fingerprints for a synthetic run.
///
/// The two in-memory streams are the quote stream (source order 0) and the
/// trade stream (source order 1); the file, row, and data fingerprints then
/// follow the exact domain-separated framing of the Python replay feed.
#[must_use]
pub fn synthetic_fingerprints(
    events: &[MarketEvent],
    sessions: &[ExchangeSession],
    options: &SyntheticDataOptions,
    symbol: &str,
) -> ReplayFingerprints {
    let mid_scale = options.base_price_scale.max(options.tick_scale);
    let half_spread = options
        .tick
        .checked_mul(Decimal18::from_i64(options.spread_ticks))
        .and_then(|value| value.checked_div(Decimal18::from_i64(2)))
        .unwrap_or(Decimal18::ZERO);
    let quote_scale = mid_scale.max(minimal_scale(half_spread));
    let trade_scale = mid_scale;

    let file = {
        let mut hasher = Sha256::new();
        hasher.update(b"fca-lake-replay-files-v1\0");
        for (order, data_type) in [(0u64, "quote"), (1u64, "trade")] {
            update_frame(
                &mut hasher,
                &canonical_fields(&[
                    "stream".to_string(),
                    order.to_string(),
                    symbol.to_string(),
                    data_type.to_string(),
                ]),
            );
        }
        hasher.finalize_hex()
    };

    let mut row = Sha256::new();
    let mut data = Sha256::new();
    data.update(b"fca-lake-replay-data-v1\0");
    update_frame(&mut data, file.as_bytes());
    for session in sessions {
        update_frame(
            &mut data,
            &canonical_fields(&[
                session.date_string(),
                timestamp_isoformat_micros(session.open_ns),
                timestamp_isoformat_micros(session.close_ns),
            ]),
        );
    }
    for event in events {
        let payload = event_framed_fields(event, options, quote_scale, trade_scale);
        update_frame(&mut row, &payload);
        update_frame(&mut data, &payload);
    }
    ReplayFingerprints {
        file,
        row: row.finalize_hex(),
        data: data.finalize_hex(),
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
    /// Number of bar events.
    pub bar_count: usize,
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
            ("bar_count", JsonValue::UInt(self.bar_count as u64)),
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
    let symbols: Vec<String> = if configuration.symbols.is_empty() {
        vec![options.symbol.clone().to_uppercase()]
    } else {
        configuration
            .symbols
            .iter()
            .map(|s| s.to_uppercase())
            .collect()
    };
    let sessions = backtest_sessions(configuration, options);
    let mut all_events = Vec::new();
    let mut primary_fingerprints: Option<ReplayFingerprints> = None;
    for (idx, symbol) in symbols.iter().enumerate() {
        let events = synthetic_events(configuration, options, symbol, &sessions)?;
        if idx == 0 {
            primary_fingerprints =
                Some(synthetic_fingerprints(&events, &sessions, options, symbol));
        }
        all_events.extend(events);
    }
    if symbols.len() > 1 {
        all_events.sort_by(|left, right| {
            left.timestamp_ns()
                .cmp(&right.timestamp_ns())
                .then_with(|| left.sequence().cmp(&right.sequence()))
        });
    }
    let fingerprints = match primary_fingerprints {
        Some(fp) => fp,
        None => ReplayFingerprints::of_events(&all_events),
    };
    Ok(ReplayFixture {
        sessions,
        events: all_events,
        source_id: "synthetic".to_string(),
        symbols,
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
    let mut ordered: Vec<(u8, MarketEvent)> = Vec::with_capacity(quotes.len() + trades.len());
    for event in quotes {
        ordered.push((0, event));
    }
    for event in trades {
        ordered.push((1, event));
    }
    // Mirrors `harness.data.replay.LakeReplayFeed`: a stable merge by
    // (timestamp, sequence, class priority, source order). Quotes carry class
    // priority 0 and trades 1, so a stable sort on the three comparable keys
    // reproduces the Python heap order.
    ordered.sort_by(|left, right| {
        left.1
            .timestamp_ns()
            .cmp(&right.1.timestamp_ns())
            .then_with(|| left.1.sequence().cmp(&right.1.sequence()))
            .then_with(|| left.0.cmp(&right.0))
    });
    Ok(ordered.into_iter().map(|(_, event)| event).collect())
}

/// Days since the Unix epoch for a proleptic Gregorian date.
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
