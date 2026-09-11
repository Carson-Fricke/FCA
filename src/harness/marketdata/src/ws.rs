//! Transport-neutral Alpaca WebSocket feeds for the live shell.
//!
//! Port of `harness.data.alpaca_ws` and `harness.data.opra_ws`. The connection
//! and clock are ports supplied by the deployment shell. The inboxes are
//! deliberately synchronous and deterministic: they perform no network,
//! clock, sleep, or random access while producing the same market-event records
//! used by replay.

use std::collections::{HashMap, VecDeque};
use std::fmt;
use std::str::FromStr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use futures_util::{SinkExt, StreamExt};
use num::Decimal18;
use serde_json::Value;

use crate::error::DataError;
use crate::events::{MarketEvent, QuoteEvent, TradeEvent};
use crate::options_universe::OptionUniverseSnapshot;
use crate::symbol::{canonical_osi_symbol, canonical_symbol};
use crate::time::{DateTime, Nanos};

/// Alpaca sent an invalid frame or rejected a stream operation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlpacaWebSocketProtocolError {
    message: String,
}

impl AlpacaWebSocketProtocolError {
    /// Build a protocol error with `message`.
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for AlpacaWebSocketProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for AlpacaWebSocketProtocolError {}

impl From<AlpacaWebSocketProtocolError> for DataError {
    fn from(error: AlpacaWebSocketProtocolError) -> Self {
        DataError::Protocol(error.message)
    }
}

fn protocol(message: impl Into<String>) -> DataError {
    DataError::Protocol(message.into())
}

/// A decoded provider market-data message.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AlpacaMessage {
    /// A top-of-book quote.
    Quote {
        /// Provider symbol field `S`.
        symbol: String,
        /// Provider timestamp field `t`.
        timestamp: String,
        /// Provider bid price field `bp`.
        bid_price: String,
        /// Provider bid size field `bs`.
        bid_size: String,
        /// Provider ask price field `ap`.
        ask_price: String,
        /// Provider ask size field `as`.
        ask_size: String,
        /// Provider venue field (`z` for SIP, `x` for OPRA/crypto).
        venue: Option<String>,
    },
    /// A trade print.
    Trade {
        /// Provider symbol field `S`.
        symbol: String,
        /// Provider timestamp field `t`.
        timestamp: String,
        /// Provider price field `p`.
        price: String,
        /// Provider size field `s`.
        size: String,
        /// Provider venue field (`x`).
        venue: Option<String>,
        /// Provider trade id field `i`.
        trade_id: Option<String>,
    },
}

/// The provider namespace one inbox normalizes for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InboxKind {
    /// Alpaca SIP equity feed.
    Sip,
    /// Alpaca crypto feed.
    Crypto,
    /// Alpaca OPRA option feed.
    Opra,
}

impl InboxKind {
    fn event_source(self) -> &'static str {
        match self {
            Self::Sip => "alpaca-sip",
            Self::Crypto => "alpaca-crypto",
            Self::Opra => "alpaca-opra",
        }
    }

    fn canonical_symbol(self, raw: &str) -> Result<String, DataError> {
        match self {
            Self::Sip => canonical_symbol(raw),
            Self::Crypto => {
                let symbol = canonical_symbol(raw)?;
                if !is_crypto_pair(&symbol) {
                    return Err(protocol(format!(
                        "Alpaca field S must use BASE/QUOTE crypto pairs: {raw:?}"
                    )));
                }
                Ok(symbol)
            }
            Self::Opra => canonical_osi_symbol(raw).map_err(|_| {
                protocol(format!(
                    "Alpaca field S must be an OSI option symbol: {raw:?}"
                ))
            }),
        }
    }
}

/// Synchronous, deterministic normalizer and kernel-facing event inbox.
pub struct AlpacaInbox {
    kind: InboxKind,
    next_sequence: u64,
    max_future_skew: Nanos,
    max_event_age: Nanos,
    events: VecDeque<MarketEvent>,
    last_observed: HashMap<String, DateTime>,
}

/// The SIP equity inbox.
pub type AlpacaSipInbox = AlpacaInbox;
/// The crypto inbox.
pub type AlpacaCryptoInbox = AlpacaInbox;
/// The OPRA option inbox.
pub type AlpacaOpraInbox = AlpacaInbox;

impl AlpacaInbox {
    /// Build a SIP inbox.
    pub fn sip(
        initial_sequence: u64,
        max_future_skew: Nanos,
        max_event_age: Nanos,
    ) -> Result<Self, DataError> {
        Self::new(
            InboxKind::Sip,
            initial_sequence,
            max_future_skew,
            max_event_age,
        )
    }

    /// Build a crypto inbox.
    pub fn crypto(
        initial_sequence: u64,
        max_future_skew: Nanos,
        max_event_age: Nanos,
    ) -> Result<Self, DataError> {
        Self::new(
            InboxKind::Crypto,
            initial_sequence,
            max_future_skew,
            max_event_age,
        )
    }

    /// Build an OPRA inbox.
    pub fn opra(
        initial_sequence: u64,
        max_future_skew: Nanos,
        max_event_age: Nanos,
    ) -> Result<Self, DataError> {
        Self::new(
            InboxKind::Opra,
            initial_sequence,
            max_future_skew,
            max_event_age,
        )
    }

    /// Build an inbox of `kind`.
    pub fn new(
        kind: InboxKind,
        initial_sequence: u64,
        max_future_skew: Nanos,
        max_event_age: Nanos,
    ) -> Result<Self, DataError> {
        if max_future_skew < 0 {
            return Err(DataError::value("max_future_skew must be non-negative"));
        }
        if max_event_age <= 0 {
            return Err(DataError::value("max_event_age must be positive"));
        }
        Ok(Self {
            kind,
            next_sequence: initial_sequence,
            max_future_skew,
            max_event_age,
            events: VecDeque::new(),
            last_observed: HashMap::new(),
        })
    }

    /// The provider namespace this inbox normalizes for.
    pub fn kind(&self) -> InboxKind {
        self.kind
    }

    /// The number of queued events.
    pub fn len(&self) -> usize {
        self.events.len()
    }

    /// Whether the inbox queue is empty.
    pub fn is_empty(&self) -> bool {
        self.events.is_empty()
    }

    /// The sequence the next accepted event will receive.
    pub fn next_sequence(&self) -> u64 {
        self.next_sequence
    }

    /// Validate and atomically enqueue one decoded provider frame.
    pub fn push(
        &mut self,
        messages: &[AlpacaMessage],
        observed_at: DateTime,
    ) -> Result<Vec<MarketEvent>, DataError> {
        if messages.is_empty() {
            return Err(protocol("Alpaca payload must not be empty"));
        }
        let mut events = Vec::with_capacity(messages.len());
        for (offset, message) in messages.iter().enumerate() {
            let sequence = self.next_sequence + offset as u64;
            events.push(self.normalize(message, observed_at, sequence)?);
        }
        for event in &events {
            self.events.push_back(event.clone());
            self.last_observed
                .insert(event.symbol().to_string(), observed_at);
        }
        self.next_sequence += events.len() as u64;
        Ok(events)
    }

    /// Remove the oldest normalized event, if one is available.
    pub fn pop(&mut self) -> Option<MarketEvent> {
        self.events.pop_front()
    }

    /// Remove events in provider order without consulting a clock.
    pub fn drain(&mut self, limit: Option<usize>) -> Result<Vec<MarketEvent>, DataError> {
        let mut drained = Vec::new();
        while !self.events.is_empty() && limit.is_none_or(|limit| drained.len() < limit) {
            if let Some(event) = self.events.pop_front() {
                drained.push(event);
            }
        }
        Ok(drained)
    }

    /// Find stale symbols using only timestamps supplied by the shell.
    pub fn stale_symbols(
        &self,
        symbols: &[String],
        observed_at: DateTime,
        active_since: DateTime,
        stale_after: Nanos,
    ) -> Result<Vec<String>, DataError> {
        if stale_after <= 0 {
            return Err(DataError::value("stale_after must be positive"));
        }
        if observed_at < active_since {
            return Err(DataError::value("observed_at cannot precede active_since"));
        }
        let mut stale = Vec::new();
        for symbol in canonical_symbols(symbols)? {
            let latest = self
                .last_observed
                .get(&symbol)
                .copied()
                .unwrap_or(active_since)
                .max(active_since);
            if observed_at.to_nanos() - latest.to_nanos() >= stale_after {
                stale.push(symbol);
            }
        }
        Ok(stale)
    }

    fn normalize(
        &self,
        message: &AlpacaMessage,
        observed_at: DateTime,
        sequence: u64,
    ) -> Result<MarketEvent, DataError> {
        match message {
            AlpacaMessage::Quote {
                symbol,
                timestamp,
                bid_price,
                bid_size,
                ask_price,
                ask_size,
                venue,
            } => {
                let symbol = self.event_symbol(symbol)?;
                let event_timestamp = self.event_timestamp(timestamp, observed_at, &symbol)?;
                let venue = null_or_text(venue.as_deref(), "z")?;
                let event_id = event_id(
                    self.kind.event_source(),
                    "q",
                    &symbol,
                    event_timestamp,
                    sequence,
                );
                Ok(MarketEvent::Quote(QuoteEvent {
                    event_id,
                    timestamp: event_timestamp,
                    sequence,
                    symbol,
                    bid_price: required_decimal(bid_price, "bp")?,
                    bid_size: required_decimal(bid_size, "bs")?,
                    ask_price: required_decimal(ask_price, "ap")?,
                    ask_size: required_decimal(ask_size, "as")?,
                    venue,
                }))
            }
            AlpacaMessage::Trade {
                symbol,
                timestamp,
                price,
                size,
                venue,
                trade_id,
            } => {
                let symbol = self.event_symbol(symbol)?;
                let event_timestamp = self.event_timestamp(timestamp, observed_at, &symbol)?;
                let venue = null_or_text(venue.as_deref(), "x")?;
                let trade_id = match trade_id {
                    None => return Err(protocol("Alpaca field i must be text or integer")),
                    Some(value) => value.clone(),
                };
                let event_id = event_id(
                    self.kind.event_source(),
                    "t",
                    &symbol,
                    event_timestamp,
                    sequence,
                );
                Ok(MarketEvent::Trade(TradeEvent {
                    event_id,
                    timestamp: event_timestamp,
                    sequence,
                    symbol,
                    price: required_decimal(price, "p")?,
                    quantity: required_decimal(size, "s")?,
                    venue,
                    trade_id: Some(trade_id),
                }))
            }
        }
    }

    fn event_symbol(&self, raw: &str) -> Result<String, DataError> {
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            return Err(protocol("Alpaca field S must be non-empty text"));
        }
        self.kind.canonical_symbol(trimmed)
    }

    fn event_timestamp(
        &self,
        raw: &str,
        observed_at: DateTime,
        symbol: &str,
    ) -> Result<DateTime, DataError> {
        let parsed = DateTime::parse_iso_utc(raw)
            .ok_or_else(|| protocol("Alpaca field t must be a timezone-aware ISO timestamp"))?;
        if parsed.to_nanos() > observed_at.to_nanos() + self.max_future_skew {
            return Err(protocol(format!(
                "Alpaca timestamp for {symbol} exceeds the allowed future skew"
            )));
        }
        if parsed.to_nanos() < observed_at.to_nanos() - self.max_event_age {
            return Err(protocol(format!(
                "Alpaca timestamp for {symbol} exceeds the maximum event age"
            )));
        }
        Ok(parsed)
    }
}

fn event_id(
    source: &str,
    message_type: &str,
    symbol: &str,
    timestamp: DateTime,
    sequence: u64,
) -> String {
    format!(
        "{source}:{message_type}:{symbol}:{}:{sequence}",
        timestamp.isoformat_micros_utc()
    )
}

/// Validate an optional provider text field exactly like Python `_optional_text`.
fn null_or_text(value: Option<&str>, field: &str) -> Result<Option<String>, DataError> {
    match value {
        None => Ok(None),
        Some(value) => {
            let trimmed = value.trim();
            if trimmed.is_empty() {
                return Err(protocol(format!(
                    "Alpaca field {field} must be non-empty text or null"
                )));
            }
            Ok(Some(trimmed.to_string()))
        }
    }
}

fn required_decimal(value: &str, field: &str) -> Result<Decimal18, DataError> {
    let parsed = Decimal18::from_str(value)
        .map_err(|_| protocol(format!("Alpaca field {field} must be an exact number")))?;
    if !parsed.is_positive() {
        return Err(protocol(format!(
            "Alpaca field {field} must be a positive finite number"
        )));
    }
    Ok(parsed)
}

fn canonical_symbols(symbols: &[String]) -> Result<Vec<String>, DataError> {
    let mut values = Vec::with_capacity(symbols.len());
    for symbol in symbols {
        values.push(canonical_symbol(symbol)?);
    }
    values.sort();
    values.dedup();
    Ok(values)
}

fn is_crypto_pair(symbol: &str) -> bool {
    let mut parts = symbol.split('/');
    let (Some(base), Some(quote), None) = (parts.next(), parts.next(), parts.next()) else {
        return false;
    };
    !base.is_empty() && !quote.is_empty()
}

/// Canonical equity channels requested by one trader deployment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlpacaSipSubscription {
    /// Canonical trade channels.
    pub trades: Vec<String>,
    /// Canonical quote channels.
    pub quotes: Vec<String>,
}

impl AlpacaSipSubscription {
    /// Build a validated SIP subscription.
    pub fn new(trades: Vec<String>, quotes: Vec<String>) -> Result<Self, DataError> {
        let trades = canonical_symbols(&trades)?;
        let quotes = canonical_symbols(&quotes)?;
        if trades.is_empty() && quotes.is_empty() {
            return Err(DataError::value(
                "at least one SIP trade or quote subscription is required",
            ));
        }
        Ok(Self { trades, quotes })
    }

    /// Every symbol monitored by the stale watchdog.
    pub fn symbols(&self) -> Vec<String> {
        let mut symbols: Vec<String> = self
            .trades
            .iter()
            .chain(self.quotes.iter())
            .cloned()
            .collect();
        symbols.sort();
        symbols.dedup();
        symbols
    }
}

/// Canonical crypto trading pairs requested by one trader deployment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlpacaCryptoSubscription {
    /// Canonical trade channels.
    pub trades: Vec<String>,
    /// Canonical quote channels.
    pub quotes: Vec<String>,
}

impl AlpacaCryptoSubscription {
    /// Build a validated crypto subscription.
    pub fn new(trades: Vec<String>, quotes: Vec<String>) -> Result<Self, DataError> {
        let base = AlpacaSipSubscription::new(trades, quotes)?;
        for symbol in base.trades.iter().chain(base.quotes.iter()) {
            if !is_crypto_pair(symbol) {
                return Err(DataError::value(format!(
                    "channels must use BASE/QUOTE crypto pairs, not {symbol:?}"
                )));
            }
        }
        Ok(Self {
            trades: base.trades,
            quotes: base.quotes,
        })
    }

    /// Every symbol monitored by the stale watchdog.
    pub fn symbols(&self) -> Vec<String> {
        let mut symbols: Vec<String> = self
            .trades
            .iter()
            .chain(self.quotes.iter())
            .cloned()
            .collect();
        symbols.sort();
        symbols.dedup();
        symbols
    }
}

/// Canonical OPRA channels for one point-in-time contract selection.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlpacaOpraSubscription {
    /// Canonical OSI trade channels.
    pub trades: Vec<String>,
    /// Canonical OSI quote channels.
    pub quotes: Vec<String>,
}

impl AlpacaOpraSubscription {
    /// Build a validated OPRA subscription.
    pub fn new(trades: Vec<String>, quotes: Vec<String>) -> Result<Self, DataError> {
        let trades = osi_symbols(&trades, "trades")?;
        let quotes = osi_symbols(&quotes, "quotes")?;
        if trades.is_empty() && quotes.is_empty() {
            return Err(DataError::value(
                "at least one OPRA trade or quote subscription is required",
            ));
        }
        Ok(Self { trades, quotes })
    }

    /// Derive channels from a point-in-time snapshot's selected contracts.
    pub fn from_snapshot(
        snapshot: &OptionUniverseSnapshot,
        include_trades: bool,
        include_quotes: bool,
    ) -> Result<Self, DataError> {
        let symbols = snapshot.symbols();
        Self::new(
            if include_trades {
                symbols.clone()
            } else {
                Vec::new()
            },
            if include_quotes { symbols } else { Vec::new() },
        )
    }

    /// Distinct underlying roots: the join key to equity streams.
    pub fn underlyings(&self) -> Vec<String> {
        let mut roots: Vec<String> = self
            .trades
            .iter()
            .chain(self.quotes.iter())
            .map(|symbol| osi_root(symbol))
            .collect();
        roots.sort();
        roots.dedup();
        roots
    }

    /// Every symbol monitored by the stale watchdog.
    pub fn symbols(&self) -> Vec<String> {
        let mut symbols: Vec<String> = self
            .trades
            .iter()
            .chain(self.quotes.iter())
            .cloned()
            .collect();
        symbols.sort();
        symbols.dedup();
        symbols
    }
}

fn osi_symbols(symbols: &[String], field_name: &str) -> Result<Vec<String>, DataError> {
    let mut values = Vec::with_capacity(symbols.len());
    for symbol in symbols {
        values.push(canonical_osi_symbol(symbol)?);
    }
    values.sort();
    let original_len = symbols.len();
    values.dedup();
    if values.len() != original_len {
        return Err(DataError::value(format!(
            "{field_name} must not contain duplicate OSI symbols"
        )));
    }
    Ok(values)
}

fn osi_root(symbol: &str) -> String {
    if symbol.len() > 15 {
        symbol[..symbol.len() - 15].to_string()
    } else {
        symbol.to_string()
    }
}

/// Channel changes that move one live OPRA subscription to another.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct OpraRotationDelta {
    /// Trade channels to remove.
    pub unsubscribe_trades: Vec<String>,
    /// Quote channels to remove.
    pub unsubscribe_quotes: Vec<String>,
    /// Trade channels to add.
    pub subscribe_trades: Vec<String>,
    /// Quote channels to add.
    pub subscribe_quotes: Vec<String>,
}

impl OpraRotationDelta {
    /// Whether the rotation requires no provider messages.
    pub fn is_empty(&self) -> bool {
        self.unsubscribe_trades.is_empty()
            && self.unsubscribe_quotes.is_empty()
            && self.subscribe_trades.is_empty()
            && self.subscribe_quotes.is_empty()
    }
}

/// Return the exact unsubscribe and subscribe sets between two selections.
pub fn rotation_delta(
    current: &AlpacaOpraSubscription,
    target: &AlpacaOpraSubscription,
) -> OpraRotationDelta {
    OpraRotationDelta {
        unsubscribe_trades: difference(&current.trades, &target.trades),
        unsubscribe_quotes: difference(&current.quotes, &target.quotes),
        subscribe_trades: difference(&target.trades, &current.trades),
        subscribe_quotes: difference(&target.quotes, &current.quotes),
    }
}

fn difference(left: &[String], right: &[String]) -> Vec<String> {
    let right: std::collections::HashSet<&str> = right.iter().map(String::as_str).collect();
    let mut result: Vec<String> = left
        .iter()
        .filter(|value| !right.contains(value.as_str()))
        .cloned()
        .collect();
    result.sort();
    result
}

/// Bounded backoff delay for the `failures`-th consecutive reconnect failure.
pub fn backoff_delay(failures: u32, base_seconds: f64, max_seconds: f64) -> f64 {
    if failures == 0 {
        return 0.0;
    }
    let exponent = (failures - 1).min(30);
    max_seconds.min(base_seconds * 2f64.powi(exponent as i32))
}

/// A synchronous stand-in for the async Alpaca shell.
///
/// The async connection loop is deployment-shell code; this port keeps the
/// deterministic pieces: channel payloads, reconnect accounting, and the
/// stale watchdog hand-off to the inbox.
pub struct AlpacaWebSocketFeed {
    subscription: AlpacaSipSubscription,
    websocket_url: String,
    backoff_seconds: f64,
    max_backoff_seconds: f64,
    connection_attempts: u32,
    stale_symbols: Vec<String>,
}

impl AlpacaWebSocketFeed {
    /// Build a SIP feed shell.
    pub fn sip(
        subscription: AlpacaSipSubscription,
        websocket_url: Option<String>,
        backoff_seconds: f64,
        max_backoff_seconds: f64,
    ) -> Result<Self, DataError> {
        Self::new(
            subscription,
            websocket_url.unwrap_or_else(|| "wss://stream.data.alpaca.markets/v2/sip".to_string()),
            backoff_seconds,
            max_backoff_seconds,
        )
    }

    /// Build a crypto feed shell.
    pub fn crypto(
        subscription: AlpacaCryptoSubscription,
        websocket_url: Option<String>,
        backoff_seconds: f64,
        max_backoff_seconds: f64,
    ) -> Result<Self, DataError> {
        Self::new(
            AlpacaSipSubscription {
                trades: subscription.trades,
                quotes: subscription.quotes,
            },
            websocket_url
                .unwrap_or_else(|| "wss://stream.data.alpaca.markets/v1beta3/crypto".to_string()),
            backoff_seconds,
            max_backoff_seconds,
        )
    }

    /// Build an OPRA feed shell.
    pub fn opra(
        subscription: AlpacaOpraSubscription,
        websocket_url: Option<String>,
        backoff_seconds: f64,
        max_backoff_seconds: f64,
    ) -> Result<Self, DataError> {
        Self::new(
            AlpacaSipSubscription {
                trades: subscription.trades,
                quotes: subscription.quotes,
            },
            websocket_url
                .unwrap_or_else(|| "wss://stream.data.alpaca.markets/v1beta1/opra".to_string()),
            backoff_seconds,
            max_backoff_seconds,
        )
    }

    fn new(
        subscription: AlpacaSipSubscription,
        websocket_url: String,
        backoff_seconds: f64,
        max_backoff_seconds: f64,
    ) -> Result<Self, DataError> {
        if websocket_url.trim().is_empty() {
            return Err(DataError::value("websocket_url must not be empty"));
        }
        if backoff_seconds < 0.0 || max_backoff_seconds < 0.0 {
            return Err(DataError::value("backoff delays must not be negative"));
        }
        Ok(Self {
            subscription,
            websocket_url,
            backoff_seconds,
            max_backoff_seconds,
            connection_attempts: 0,
            stale_symbols: Vec::new(),
        })
    }

    /// The websocket URL.
    pub fn websocket_url(&self) -> &str {
        &self.websocket_url
    }

    /// The number of connection attempts made.
    pub fn connection_attempts(&self) -> u32 {
        self.connection_attempts
    }

    /// The most recent watchdog result.
    pub fn stale_symbols(&self) -> &[String] {
        &self.stale_symbols
    }

    /// Register one connection attempt.
    pub fn begin_attempt(&mut self) {
        self.connection_attempts += 1;
    }

    /// The delay before the next reconnect attempt.
    pub fn next_backoff(&self) -> f64 {
        backoff_delay(
            self.connection_attempts,
            self.backoff_seconds,
            self.max_backoff_seconds,
        )
    }

    /// The auth control payload as compact JSON.
    pub fn auth_payload(&self, api_key: &str, api_secret: &str) -> String {
        format!(
            "{{\"action\":\"auth\",\"key\":{},\"secret\":{}}}",
            json_string(api_key),
            json_string(api_secret)
        )
    }

    /// The subscribe control payload as compact JSON.
    pub fn subscribe_payload(&self) -> String {
        format!(
            "{{\"action\":\"subscribe\",\"trades\":{},\"quotes\":{}}}",
            json_string_array(&self.subscription.trades),
            json_string_array(&self.subscription.quotes)
        )
    }

    /// Rotate the monitored channel set and return the delta.
    pub fn rotate(&mut self, target: &AlpacaSipSubscription) -> OpraRotationDelta {
        let current = AlpacaOpraSubscription {
            trades: self.subscription.trades.clone(),
            quotes: self.subscription.quotes.clone(),
        };
        let target_opra = AlpacaOpraSubscription {
            trades: target.trades.clone(),
            quotes: target.quotes.clone(),
        };
        let delta = rotation_delta(&current, &target_opra);
        self.subscription = target.clone();
        self.stale_symbols.clear();
        delta
    }

    /// Run the stale watchdog against the current subscription.
    pub fn check_stale(
        &mut self,
        inbox: &AlpacaInbox,
        observed_at: DateTime,
        active_since: DateTime,
        stale_after: Nanos,
    ) -> Result<&[String], DataError> {
        self.stale_symbols = inbox.stale_symbols(
            &self.subscription.symbols(),
            observed_at,
            active_since,
            stale_after,
        )?;
        Ok(&self.stale_symbols)
    }
}

fn json_string(value: &str) -> String {
    let mut output = String::with_capacity(value.len() + 2);
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            other => output.push(other),
        }
    }
    output.push('"');
    output
}

fn json_string_array(values: &[String]) -> String {
    let items: Vec<String> = values.iter().map(|value| json_string(value)).collect();
    format!("[{}]", items.join(","))
}

/// A decoded-frame connection implemented by a network adapter.
#[async_trait]
pub trait AsyncWebSocketConnection: Send + Sync {
    /// Send one JSON text message.
    async fn send(&self, message: &str) -> Result<(), DataError>;
    /// Receive one frame, returning `None` when the timeout expires.
    async fn receive(&self, timeout_seconds: f64) -> Result<Option<Vec<u8>>, DataError>;
    /// Close the connection.
    async fn close(&self) -> Result<(), DataError>;
}

/// Connection factory kept outside the deterministic market-data kernel.
#[async_trait]
pub trait AsyncWebSocketTransport: Send + Sync {
    /// Open one connection to `url`.
    async fn connect(&self, url: &str) -> Result<Arc<dyn AsyncWebSocketConnection>, DataError>;
}

/// Provider clock supplied by the async shell.
pub type AsyncClock = Arc<dyn Fn() -> DateTime + Send + Sync>;
/// Stale-symbol notification sink supplied by the shell.
pub type StaleHandler = Arc<dyn Fn(&[String]) + Send + Sync>;

/// Cooperative shutdown signal for the async feed loop.
#[derive(Clone, Default)]
pub struct StopSignal(Arc<AtomicBool>);

impl StopSignal {
    /// Build an unset signal.
    pub fn new() -> Self {
        Self::default()
    }

    /// Request the loop stop at its next checkpoint.
    pub fn stop(&self) {
        self.0.store(true, Ordering::SeqCst);
    }

    /// Whether a stop has been requested.
    pub fn is_set(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }
}

/// Configuration for one async Alpaca live feed.
pub struct AlpacaLiveConfig {
    /// Provider API key.
    pub api_key: String,
    /// Provider API secret.
    pub api_secret: String,
    /// Provider namespace, which selects the default endpoint.
    pub kind: InboxKind,
    /// Canonical channels to subscribe.
    pub subscription: AlpacaSipSubscription,
    /// Network transport adapter.
    pub transport: Arc<dyn AsyncWebSocketTransport>,
    /// Kernel-facing event inbox.
    pub inbox: AlpacaInbox,
    /// Injected shell clock.
    pub clock: AsyncClock,
    /// Explicit endpoint, or the kind's production default.
    pub websocket_url: Option<String>,
    /// First backoff delay.
    pub backoff_seconds: f64,
    /// Maximum backoff delay.
    pub max_backoff_seconds: f64,
    /// Receive/watchdog interval.
    pub watchdog_interval: f64,
    /// Staleness threshold.
    pub stale_after: Nanos,
    /// Handshake timeout.
    pub handshake_timeout: Nanos,
    /// Optional stale-symbol sink.
    pub on_stale: Option<StaleHandler>,
}

/// Async Alpaca shell with auth, reconnect backoff, and stale monitoring.
///
/// The connection and clock are ports supplied by the deployment shell. The
/// loop mirrors `harness.data.alpaca_ws.AlpacaSipWebSocketFeed`: authenticate,
/// subscribe, then stream and watchdog until stopped, reconnecting failures
/// with bounded exponential backoff.
pub struct AlpacaLiveFeed {
    api_key: String,
    api_secret: String,
    subscription: AlpacaSipSubscription,
    transport: Arc<dyn AsyncWebSocketTransport>,
    inbox: AlpacaInbox,
    clock: AsyncClock,
    websocket_url: String,
    backoff_seconds: f64,
    max_backoff_seconds: f64,
    watchdog_interval: f64,
    stale_after: Nanos,
    handshake_timeout: Nanos,
    on_stale: Option<StaleHandler>,
    connection: Arc<tokio::sync::Mutex<Option<Arc<dyn AsyncWebSocketConnection>>>>,
    stale_symbols: Vec<String>,
    connection_attempts: u32,
    last_error: Option<String>,
}

impl AlpacaLiveFeed {
    /// Build a validated async feed.
    pub fn new(config: AlpacaLiveConfig) -> Result<Self, DataError> {
        if config.api_key.trim().is_empty() {
            return Err(DataError::value("api_key must not be empty"));
        }
        if config.api_secret.trim().is_empty() {
            return Err(DataError::value("api_secret must not be empty"));
        }
        if config.backoff_seconds < 0.0 || config.max_backoff_seconds < 0.0 {
            return Err(DataError::value("backoff delays must not be negative"));
        }
        if config.watchdog_interval <= 0.0 {
            return Err(DataError::value("watchdog_interval must be positive"));
        }
        if config.stale_after <= 0 {
            return Err(DataError::value("stale_after must be positive"));
        }
        if config.handshake_timeout <= 0 {
            return Err(DataError::value("handshake_timeout must be positive"));
        }
        let websocket_url = match config.websocket_url {
            Some(url) if !url.trim().is_empty() => url.trim_end_matches('/').to_string(),
            Some(_) => return Err(DataError::value("websocket_url must not be empty")),
            None => default_websocket_url(config.kind).to_string(),
        };
        Ok(Self {
            api_key: config.api_key,
            api_secret: config.api_secret,
            subscription: config.subscription,
            transport: config.transport,
            inbox: config.inbox,
            clock: config.clock,
            websocket_url,
            backoff_seconds: config.backoff_seconds,
            max_backoff_seconds: config.max_backoff_seconds,
            watchdog_interval: config.watchdog_interval,
            stale_after: config.stale_after,
            handshake_timeout: config.handshake_timeout,
            on_stale: config.on_stale,
            connection: Arc::new(tokio::sync::Mutex::new(None)),
            stale_symbols: Vec::new(),
            connection_attempts: 0,
            last_error: None,
        })
    }

    /// The inbox the feed fills.
    pub fn inbox(&self) -> &AlpacaInbox {
        &self.inbox
    }

    /// The inbox the feed fills, mutably.
    pub fn inbox_mut(&mut self) -> &mut AlpacaInbox {
        &mut self.inbox
    }

    /// The endpoint in use.
    pub fn websocket_url(&self) -> &str {
        &self.websocket_url
    }

    /// The number of connection attempts made.
    pub fn connection_attempts(&self) -> u32 {
        self.connection_attempts
    }

    /// The most recent connection error, if any.
    pub fn last_error(&self) -> Option<&str> {
        self.last_error.as_deref()
    }

    /// The most recent watchdog result.
    pub fn stale_symbols(&self) -> &[String] {
        &self.stale_symbols
    }

    /// Replace the subscribed channels and reset staleness tracking.
    pub fn set_subscription(&mut self, subscription: AlpacaSipSubscription) {
        self.subscription = subscription;
        self.stale_symbols.clear();
    }

    /// The most recent live connection, if one is established.
    pub async fn live_connection(&self) -> Option<Arc<dyn AsyncWebSocketConnection>> {
        self.connection.lock().await.clone()
    }

    /// Consume until stopped, reconnecting failures with bounded backoff.
    pub async fn run(
        &mut self,
        stop: StopSignal,
        max_connections: Option<u32>,
    ) -> Result<(), DataError> {
        if let Some(max) = max_connections {
            if max < 1 {
                return Err(DataError::value(
                    "max_connections must be at least 1 or None",
                ));
            }
        }
        let mut failures: u32 = 0;
        while !stop.is_set() {
            if let Some(max) = max_connections {
                if self.connection_attempts >= max {
                    return match self.last_error.take() {
                        Some(error) => Err(DataError::Protocol(error)),
                        None => Ok(()),
                    };
                }
            }
            self.connection_attempts += 1;
            match self.run_connection(&stop).await {
                Ok(()) => return Ok(()),
                Err(error) => {
                    self.last_error = Some(error.to_string());
                    failures += 1;
                    if stop.is_set() {
                        return Ok(());
                    }
                    if let Some(max) = max_connections {
                        if self.connection_attempts >= max {
                            return Err(error);
                        }
                    }
                    let delay =
                        backoff_delay(failures, self.backoff_seconds, self.max_backoff_seconds);
                    tokio::time::sleep(Duration::from_secs_f64(delay.max(0.0))).await;
                }
            }
        }
        Ok(())
    }

    async fn run_connection(&mut self, stop: &StopSignal) -> Result<(), DataError> {
        let connection = self.transport.connect(&self.websocket_url).await?;
        *self.connection.lock().await = Some(Arc::clone(&connection));
        let result = self.stream_connection(&connection, stop).await;
        *self.connection.lock().await = None;
        let _ = connection.close().await;
        result
    }

    async fn stream_connection(
        &mut self,
        connection: &Arc<dyn AsyncWebSocketConnection>,
        stop: &StopSignal,
    ) -> Result<(), DataError> {
        if !self.await_control(connection, stop, "connected").await? {
            return Ok(());
        }
        connection
            .send(&auth_payload_string(&self.api_key, &self.api_secret))
            .await?;
        if !self
            .await_control(connection, stop, "authenticated")
            .await?
        {
            return Ok(());
        }
        connection
            .send(&subscribe_payload_string(&self.subscription))
            .await?;
        if !self.await_subscription(connection, stop).await? {
            return Ok(());
        }
        let active_since = self.now();
        self.stale_symbols.clear();
        while !stop.is_set() {
            if let Some(messages) = self
                .receive_json(connection, self.watchdog_interval)
                .await?
            {
                let market = market_messages(&messages)?;
                if !market.is_empty() {
                    let decoded = decode_market_messages(&market)?;
                    self.inbox.push(&decoded, self.now())?;
                }
            }
            self.check_stale(active_since)?;
        }
        Ok(())
    }

    async fn await_control(
        &mut self,
        connection: &Arc<dyn AsyncWebSocketConnection>,
        stop: &StopSignal,
        expected: &str,
    ) -> Result<bool, DataError> {
        let started = self.now();
        while !stop.is_set() {
            if let Some(messages) = self
                .receive_json(connection, self.watchdog_interval)
                .await?
            {
                for message in &messages {
                    raise_provider_error(message)?;
                    if message.get("T").and_then(Value::as_str) == Some("success")
                        && message.get("msg").and_then(Value::as_str) == Some(expected)
                    {
                        return Ok(true);
                    }
                }
            }
            if self.now().to_nanos() - started.to_nanos() >= self.handshake_timeout {
                return Err(protocol(format!(
                    "timed out waiting for Alpaca {expected} response"
                )));
            }
        }
        Ok(false)
    }

    async fn await_subscription(
        &mut self,
        connection: &Arc<dyn AsyncWebSocketConnection>,
        stop: &StopSignal,
    ) -> Result<bool, DataError> {
        let started = self.now();
        while !stop.is_set() {
            if let Some(messages) = self
                .receive_json(connection, self.watchdog_interval)
                .await?
            {
                let mut subscribed = false;
                for message in &messages {
                    raise_provider_error(message)?;
                    if message.get("T").and_then(Value::as_str) == Some("subscription") {
                        let trades = control_symbols(message.get("trades"), "trades")?;
                        let quotes = control_symbols(message.get("quotes"), "quotes")?;
                        if trades != self.subscription.trades {
                            return Err(protocol(
                                "Alpaca acknowledged unexpected trade subscriptions",
                            ));
                        }
                        if quotes != self.subscription.quotes {
                            return Err(protocol(
                                "Alpaca acknowledged unexpected quote subscriptions",
                            ));
                        }
                        subscribed = true;
                    }
                }
                if subscribed {
                    let market = market_messages(&messages)?;
                    if !market.is_empty() {
                        let decoded = decode_market_messages(&market)?;
                        self.inbox.push(&decoded, self.now())?;
                    }
                    return Ok(true);
                }
            }
            if self.now().to_nanos() - started.to_nanos() >= self.handshake_timeout {
                return Err(protocol(
                    "timed out waiting for Alpaca subscription response",
                ));
            }
        }
        Ok(false)
    }

    fn check_stale(&mut self, active_since: DateTime) -> Result<(), DataError> {
        let observed = self.now();
        let stale = self.inbox.stale_symbols(
            &self.subscription.symbols(),
            observed,
            active_since,
            self.stale_after,
        )?;
        let changed = stale != self.stale_symbols;
        self.stale_symbols = stale;
        if changed && !self.stale_symbols.is_empty() {
            if let Some(handler) = &self.on_stale {
                handler(&self.stale_symbols);
            }
        }
        Ok(())
    }

    fn now(&self) -> DateTime {
        (self.clock)()
    }

    async fn receive_json(
        &self,
        connection: &Arc<dyn AsyncWebSocketConnection>,
        timeout_seconds: f64,
    ) -> Result<Option<Vec<Value>>, DataError> {
        let Some(frame) = connection.receive(timeout_seconds).await? else {
            return Ok(None);
        };
        let text = String::from_utf8(frame)
            .map_err(|_| protocol("Alpaca WebSocket frame is not UTF-8"))?;
        let payload: Value = serde_json::from_str(&text)
            .map_err(|_| protocol("Alpaca WebSocket frame contains invalid JSON"))?;
        let Some(array) = payload.as_array() else {
            return Err(protocol(
                "Alpaca WebSocket frame must be a non-empty JSON array",
            ));
        };
        if array.is_empty() {
            return Err(protocol(
                "Alpaca WebSocket frame must be a non-empty JSON array",
            ));
        }
        if !array.iter().all(Value::is_object) {
            return Err(protocol("Alpaca WebSocket frame must contain JSON objects"));
        }
        Ok(Some(array.clone()))
    }

    /// The auth control payload as compact JSON.
    pub fn auth_payload(&self) -> String {
        auth_payload_string(&self.api_key, &self.api_secret)
    }

    /// The subscribe control payload as compact JSON.
    pub fn subscribe_payload(&self) -> String {
        subscribe_payload_string(&self.subscription)
    }
}

/// Async OPRA feed that rotates channel sets without reconnecting.
pub struct AlpacaOpraLiveFeed {
    feed: AlpacaLiveFeed,
    subscription: AlpacaOpraSubscription,
}

impl AlpacaOpraLiveFeed {
    /// Wrap an async feed with an OPRA channel selection.
    pub fn new(mut feed: AlpacaLiveFeed, subscription: AlpacaOpraSubscription) -> Self {
        feed.set_subscription(AlpacaSipSubscription {
            trades: subscription.trades.clone(),
            quotes: subscription.quotes.clone(),
        });
        Self { feed, subscription }
    }

    /// The underlying async feed.
    pub fn feed(&self) -> &AlpacaLiveFeed {
        &self.feed
    }

    /// The currently subscribed OPRA channels.
    pub fn subscription(&self) -> &AlpacaOpraSubscription {
        &self.subscription
    }

    /// Run the underlying feed loop.
    pub async fn run(
        &mut self,
        stop: StopSignal,
        max_connections: Option<u32>,
    ) -> Result<(), DataError> {
        self.feed.run(stop, max_connections).await
    }

    /// Swap the live channel sets without reconnecting.
    pub async fn rotate(
        &mut self,
        subscription: AlpacaOpraSubscription,
    ) -> Result<OpraRotationDelta, DataError> {
        let delta = rotation_delta(&self.subscription, &subscription);
        let connection = self
            .feed
            .live_connection()
            .await
            .ok_or_else(|| protocol("cannot rotate an OPRA subscription before connecting"))?;
        for payload in opra_rotation_payloads(&delta) {
            connection.send(&payload).await?;
        }
        self.feed.set_subscription(AlpacaSipSubscription {
            trades: subscription.trades.clone(),
            quotes: subscription.quotes.clone(),
        });
        self.subscription = subscription;
        Ok(delta)
    }
}

fn default_websocket_url(kind: InboxKind) -> &'static str {
    match kind {
        InboxKind::Sip => "wss://stream.data.alpaca.markets/v2/sip",
        InboxKind::Crypto => "wss://stream.data.alpaca.markets/v1beta3/crypto",
        InboxKind::Opra => "wss://stream.data.alpaca.markets/v1beta1/opra",
    }
}

fn auth_payload_string(api_key: &str, api_secret: &str) -> String {
    format!(
        "{{\"action\":\"auth\",\"key\":{},\"secret\":{}}}",
        json_string(api_key),
        json_string(api_secret)
    )
}

fn subscribe_payload_string(subscription: &AlpacaSipSubscription) -> String {
    format!(
        "{{\"action\":\"subscribe\",\"trades\":{},\"quotes\":{}}}",
        json_string_array(&subscription.trades),
        json_string_array(&subscription.quotes)
    )
}

/// The rotation payloads one OPRA channel change requires, in send order.
pub fn opra_rotation_payloads(delta: &OpraRotationDelta) -> Vec<String> {
    let mut payloads = Vec::new();
    if !delta.unsubscribe_trades.is_empty() || !delta.unsubscribe_quotes.is_empty() {
        payloads.push(format!(
            "{{\"action\":\"unsubscribe\",\"trades\":{},\"quotes\":{}}}",
            json_string_array(&delta.unsubscribe_trades),
            json_string_array(&delta.unsubscribe_quotes)
        ));
    }
    if !delta.subscribe_trades.is_empty() || !delta.subscribe_quotes.is_empty() {
        payloads.push(format!(
            "{{\"action\":\"subscribe\",\"trades\":{},\"quotes\":{}}}",
            json_string_array(&delta.subscribe_trades),
            json_string_array(&delta.subscribe_quotes)
        ));
    }
    payloads
}

fn market_messages(messages: &[Value]) -> Result<Vec<Value>, DataError> {
    let mut market = Vec::new();
    for message in messages {
        raise_provider_error(message)?;
        match message.get("T").and_then(Value::as_str) {
            Some("q") | Some("t") => market.push(message.clone()),
            Some("success") | Some("subscription") => {}
            other => {
                return Err(protocol(format!(
                    "unexpected Alpaca message type: {other:?}"
                )))
            }
        }
    }
    Ok(market)
}

fn raise_provider_error(message: &Value) -> Result<(), DataError> {
    if message.get("T").and_then(Value::as_str) != Some("error") {
        return Ok(());
    }
    let code = message.get("code").cloned().unwrap_or(Value::Null);
    let detail = message.get("msg").cloned().unwrap_or(Value::Null);
    Err(protocol(format!("Alpaca stream error {code}: {detail}")))
}

fn control_symbols(value: Option<&Value>, field: &str) -> Result<Vec<String>, DataError> {
    let Some(Value::Array(items)) = value else {
        return Err(protocol(format!(
            "Alpaca subscription field {field} must be a symbol list"
        )));
    };
    let mut values = Vec::with_capacity(items.len());
    for item in items {
        let Some(text) = item.as_str() else {
            return Err(protocol(format!(
                "Alpaca subscription field {field} must be a symbol list"
            )));
        };
        values.push(canonical_symbol(text)?);
    }
    let original = values.len();
    values.sort();
    values.dedup();
    if values.len() != original {
        return Err(protocol(format!(
            "Alpaca subscription field {field} must not contain duplicates"
        )));
    }
    Ok(values)
}

fn decode_market_messages(messages: &[Value]) -> Result<Vec<AlpacaMessage>, DataError> {
    let mut decoded = Vec::with_capacity(messages.len());
    for message in messages {
        match message.get("T").and_then(Value::as_str).unwrap_or_default() {
            "q" => decoded.push(AlpacaMessage::Quote {
                symbol: required_text(message, "S")?,
                timestamp: required_text(message, "t")?,
                bid_price: number_text(message, "bp")?,
                bid_size: number_text(message, "bs")?,
                ask_price: number_text(message, "ap")?,
                ask_size: number_text(message, "as")?,
                venue: match optional_text_field(message, "z")? {
                    Some(venue) => Some(venue),
                    None => optional_text_field(message, "x")?,
                },
            }),
            "t" => decoded.push(AlpacaMessage::Trade {
                symbol: required_text(message, "S")?,
                timestamp: required_text(message, "t")?,
                price: number_text(message, "p")?,
                size: number_text(message, "s")?,
                venue: optional_text_field(message, "x")?,
                trade_id: Some(trade_id_text(message, "i")?),
            }),
            _ => return Err(protocol("Alpaca market message must be a quote or trade")),
        }
    }
    Ok(decoded)
}

fn required_text(message: &Value, field: &str) -> Result<String, DataError> {
    match message.get(field).and_then(Value::as_str) {
        Some(value) if !value.trim().is_empty() => Ok(value.trim().to_string()),
        _ => Err(protocol(format!(
            "Alpaca field {field} must be non-empty text"
        ))),
    }
}

fn optional_text_field(message: &Value, field: &str) -> Result<Option<String>, DataError> {
    match message.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) if !value.trim().is_empty() => {
            Ok(Some(value.trim().to_string()))
        }
        _ => Err(protocol(format!(
            "Alpaca field {field} must be non-empty text or null"
        ))),
    }
}

fn number_text(message: &Value, field: &str) -> Result<String, DataError> {
    match message.get(field) {
        Some(Value::Number(number)) => Ok(number.to_string()),
        Some(Value::String(text)) if !text.trim().is_empty() => Ok(text.trim().to_string()),
        _ => Err(protocol(format!(
            "Alpaca field {field} must be an exact number"
        ))),
    }
}

fn trade_id_text(message: &Value, field: &str) -> Result<String, DataError> {
    match message.get(field) {
        Some(Value::Number(number)) => Ok(number.to_string()),
        Some(Value::String(text)) => Ok(text.clone()),
        _ => Err(protocol("Alpaca field i must be text or integer")),
    }
}

/// A real network transport backed by `tokio-tungstenite`.
pub struct TungsteniteTransport;

impl TungsteniteTransport {
    /// Build a transport.
    pub fn new() -> Self {
        Self
    }
}

impl Default for TungsteniteTransport {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl AsyncWebSocketTransport for TungsteniteTransport {
    async fn connect(&self, url: &str) -> Result<Arc<dyn AsyncWebSocketConnection>, DataError> {
        let (stream, _) = tokio_tungstenite::connect_async(url)
            .await
            .map_err(|error| DataError::Protocol(format!("websocket connect: {error}")))?;
        Ok(Arc::new(TungsteniteConnection {
            stream: tokio::sync::Mutex::new(stream),
        }))
    }
}

struct TungsteniteConnection {
    stream: tokio::sync::Mutex<
        tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
        >,
    >,
}

#[async_trait]
impl AsyncWebSocketConnection for TungsteniteConnection {
    async fn send(&self, message: &str) -> Result<(), DataError> {
        let mut stream = self.stream.lock().await;
        stream
            .send(tokio_tungstenite::tungstenite::Message::Text(
                message.to_string().into(),
            ))
            .await
            .map_err(|error| DataError::Protocol(format!("websocket send: {error}")))
    }

    async fn receive(&self, timeout_seconds: f64) -> Result<Option<Vec<u8>>, DataError> {
        let timeout = Duration::from_secs_f64(timeout_seconds.max(0.0));
        let mut stream = self.stream.lock().await;
        loop {
            match tokio::time::timeout(timeout, stream.next()).await {
                Err(_) => return Ok(None),
                Ok(None) => return Err(DataError::Protocol("websocket closed".to_string())),
                Ok(Some(Err(error))) => {
                    return Err(DataError::Protocol(format!("websocket receive: {error}")))
                }
                Ok(Some(Ok(tokio_tungstenite::tungstenite::Message::Text(text)))) => {
                    return Ok(Some(text.as_bytes().to_vec()))
                }
                Ok(Some(Ok(tokio_tungstenite::tungstenite::Message::Binary(bytes)))) => {
                    return Ok(Some(bytes.to_vec()))
                }
                Ok(Some(Ok(tokio_tungstenite::tungstenite::Message::Close(_)))) => {
                    return Err(DataError::Protocol("websocket closed".to_string()))
                }
                Ok(Some(Ok(_))) => continue,
            }
        }
    }

    async fn close(&self) -> Result<(), DataError> {
        let mut stream = self.stream.lock().await;
        let _ = stream.close(None).await;
        Ok(())
    }
}

/// The SIP feed shell.
pub type AlpacaSipWebSocketFeed = AlpacaWebSocketFeed;
/// The crypto feed shell.
pub type AlpacaCryptoWebSocketFeed = AlpacaWebSocketFeed;
/// The OPRA feed shell.
pub type AlpacaOpraWebSocketFeed = AlpacaWebSocketFeed;

#[cfg(test)]
mod tests {
    use super::*;

    fn instant(hour: i64) -> DateTime {
        DateTime::from_micros(hour * 3_600 * 1_000_000)
    }

    #[test]
    fn sip_inbox_normalizes_quote_and_sequence() {
        let mut inbox = AlpacaInbox::sip(0, 5_000_000_000, 300_000_000_000).unwrap();
        let messages = vec![AlpacaMessage::Quote {
            symbol: "aapl".to_string(),
            timestamp: instant(0).isoformat_micros() + "Z",
            bid_price: "10.5".to_string(),
            bid_size: "100".to_string(),
            ask_price: "10.6".to_string(),
            ask_size: "200".to_string(),
            venue: Some("V".to_string()),
        }];
        let events = inbox.push(&messages, instant(0)).unwrap();
        assert_eq!(events.len(), 1);
        match &events[0] {
            MarketEvent::Quote(quote) => {
                assert_eq!(quote.symbol, "AAPL");
                assert_eq!(quote.sequence, 0);
                assert!(quote.event_id.starts_with("alpaca-sip:q:AAPL:"));
            }
            _ => panic!("expected quote"),
        }
        assert_eq!(inbox.next_sequence(), 1);
    }

    #[test]
    fn opra_subscription_rotates_to_snapshot() {
        let current =
            AlpacaOpraSubscription::new(vec!["AAPL240119C00150000".to_string()], Vec::new())
                .unwrap();
        let target =
            AlpacaOpraSubscription::new(vec!["AAPL240119C00160000".to_string()], Vec::new())
                .unwrap();
        let delta = rotation_delta(&current, &target);
        assert!(!delta.is_empty());
        assert_eq!(delta.unsubscribe_trades, vec!["AAPL240119C00150000"]);
        assert_eq!(delta.subscribe_trades, vec!["AAPL240119C00160000"]);
    }

    #[test]
    fn backoff_is_bounded() {
        assert_eq!(backoff_delay(0, 0.5, 30.0), 0.0);
        assert_eq!(backoff_delay(1, 0.5, 30.0), 0.5);
        assert_eq!(backoff_delay(3, 0.5, 30.0), 2.0);
        assert_eq!(backoff_delay(40, 0.5, 30.0), 30.0);
    }

    #[test]
    fn opra_rotation_payloads_match_python() {
        let current =
            AlpacaOpraSubscription::new(vec!["AAPL240119C00150000".to_string()], Vec::new())
                .unwrap();
        let target =
            AlpacaOpraSubscription::new(vec!["AAPL240119C00160000".to_string()], Vec::new())
                .unwrap();
        let payloads = opra_rotation_payloads(&rotation_delta(&current, &target));
        assert_eq!(
            payloads[0],
            "{\"action\":\"unsubscribe\",\"trades\":[\"AAPL240119C00150000\"],\"quotes\":[]}"
        );
        assert_eq!(
            payloads[1],
            "{\"action\":\"subscribe\",\"trades\":[\"AAPL240119C00160000\"],\"quotes\":[]}"
        );
    }

    struct FakeConnection {
        frames: std::sync::Mutex<VecDeque<Vec<u8>>>,
        sent: std::sync::Mutex<Vec<String>>,
    }

    impl FakeConnection {
        fn new(frames: Vec<Vec<u8>>) -> Self {
            Self {
                frames: std::sync::Mutex::new(frames.into()),
                sent: std::sync::Mutex::new(Vec::new()),
            }
        }
    }

    #[async_trait]
    impl AsyncWebSocketConnection for FakeConnection {
        async fn send(&self, message: &str) -> Result<(), DataError> {
            self.sent.lock().unwrap().push(message.to_string());
            Ok(())
        }

        async fn receive(&self, _timeout_seconds: f64) -> Result<Option<Vec<u8>>, DataError> {
            match self.frames.lock().unwrap().pop_front() {
                Some(frame) => Ok(Some(frame)),
                None => Err(DataError::Protocol("fake closed".to_string())),
            }
        }

        async fn close(&self) -> Result<(), DataError> {
            Ok(())
        }
    }

    struct FakeTransport {
        connection: Arc<FakeConnection>,
    }

    #[async_trait]
    impl AsyncWebSocketTransport for FakeTransport {
        async fn connect(
            &self,
            _url: &str,
        ) -> Result<Arc<dyn AsyncWebSocketConnection>, DataError> {
            Ok(self.connection.clone())
        }
    }

    #[tokio::test]
    async fn live_feed_authenticates_subscribes_and_streams() {
        let stamp = DateTime::from_micros(0).isoformat_micros_utc();
        let frames = vec![
            serde_json::to_vec(&serde_json::json!([{"T": "success", "msg": "connected"}])).unwrap(),
            serde_json::to_vec(&serde_json::json!([{"T": "success", "msg": "authenticated"}]))
                .unwrap(),
            serde_json::to_vec(&serde_json::json!([
                {"T": "subscription", "trades": ["AAPL"], "quotes": ["AAPL"]},
                {"T": "t", "S": "AAPL", "t": stamp, "p": "10.5", "s": "100", "x": "V", "i": "7"}
            ]))
            .unwrap(),
        ];
        let connection = Arc::new(FakeConnection::new(frames));
        let transport = Arc::new(FakeTransport {
            connection: Arc::clone(&connection),
        });
        let inbox = AlpacaInbox::sip(0, 5_000_000_000, 300_000_000_000).unwrap();
        let clock: AsyncClock = Arc::new(|| DateTime::from_micros(0));
        let config = AlpacaLiveConfig {
            api_key: "key".to_string(),
            api_secret: "secret".to_string(),
            kind: InboxKind::Sip,
            subscription: AlpacaSipSubscription::new(
                vec!["AAPL".to_string()],
                vec!["AAPL".to_string()],
            )
            .unwrap(),
            transport,
            inbox,
            clock,
            websocket_url: None,
            backoff_seconds: 0.5,
            max_backoff_seconds: 30.0,
            watchdog_interval: 1.0,
            stale_after: 30_000_000_000,
            handshake_timeout: 10_000_000_000,
            on_stale: None,
        };
        let mut feed = AlpacaLiveFeed::new(config).unwrap();
        let stop = StopSignal::new();
        let error = feed.run_connection(&stop).await.unwrap_err();
        assert_eq!(error.to_string(), "fake closed");
        assert_eq!(feed.inbox().len(), 1);
        let sent = connection.sent.lock().unwrap().clone();
        assert_eq!(
            sent[0],
            "{\"action\":\"auth\",\"key\":\"key\",\"secret\":\"secret\"}"
        );
        assert_eq!(
            sent[1],
            "{\"action\":\"subscribe\",\"trades\":[\"AAPL\"],\"quotes\":[\"AAPL\"]}"
        );
        match feed.inbox_mut().pop().unwrap() {
            MarketEvent::Trade(trade) => {
                assert_eq!(trade.symbol, "AAPL");
                assert_eq!(trade.trade_id.as_deref(), Some("7"));
                assert_eq!(trade.venue.as_deref(), Some("V"));
            }
            _ => panic!("expected trade"),
        }
    }
}
