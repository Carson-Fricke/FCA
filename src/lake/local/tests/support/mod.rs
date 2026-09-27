#![allow(dead_code)]

//! Shared fakes and fixtures for the local integration tests.

use std::collections::BTreeMap;
use std::sync::Mutex;
use std::time::Duration;

use futures_util::future::BoxFuture;
use lake_configuration::{LakeSettings, UniverseSettings};
use lake_contracts::{Date, DateTime, Decimal18};
use lake_ingestion::{CryptoDataProvider, EquityDataProvider};
use lake_providers::{
    NormalizedCryptoQuote, NormalizedCryptoTrade, NormalizedQuote, NormalizedTrade, ProviderError,
};

/// Build a calendar date.
pub fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

/// Build a UTC instant at whole seconds.
pub fn instant(year: i32, month: u32, day_of_month: u32, hour: u32, minute: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day_of_month, hour, minute, 0, 0).expect("valid datetime")
}

/// The fake provider credentials the pinned tests use.
pub fn credentials() -> BTreeMap<String, String> {
    BTreeMap::from([
        ("LAKE_PROVIDER__API_KEY".to_string(), "fake-key".to_string()),
        (
            "LAKE_PROVIDER__API_SECRET".to_string(),
            "fake-secret".to_string(),
        ),
    ])
}

/// Load the repository lake settings with the supplied environment overrides.
pub fn load_settings(environ: &BTreeMap<String, String>) -> LakeSettings {
    LakeSettings::load(None, environ).expect("settings load")
}

/// Load the repository soak universes with the supplied environment overrides.
pub fn load_universes(environ: &BTreeMap<String, String>) -> UniverseSettings {
    UniverseSettings::load(None, environ).expect("universes load")
}

/// Instrumented equity provider fake.
#[derive(Default)]
pub struct FakeAlpaca {
    calls: Mutex<Vec<(String, String, Option<Date>)>>,
    in_flight: Mutex<u32>,
    max_in_flight: Mutex<u32>,
    poisoned: Vec<Date>,
    poison_message: Option<String>,
    blips: Mutex<BTreeMap<Date, u32>>,
    delay: Option<Duration>,
}

impl FakeAlpaca {
    /// An instrumented provider that always succeeds promptly.
    pub fn new() -> Self {
        Self::default()
    }

    /// Fail every trade fetch for `date` permanently.
    #[must_use]
    pub fn poisoning(mut self, date: Date) -> Self {
        self.poisoned.push(date);
        self.poison_message = Some("poisoned session".to_string());
        self
    }

    /// Fail the first `failures` trade fetches for `date` at the transport.
    pub fn blipping(self, date: Date, failures: u32) -> Self {
        self.blips.lock().expect("blips").insert(date, failures);
        self
    }

    /// Delay every fetch, so concurrency becomes observable.
    pub fn slow(mut self, delay: Duration) -> Self {
        self.delay = Some(delay);
        self
    }

    /// The `(kind, symbol)` calls observed so far, in order.
    pub fn calls(&self) -> Vec<(String, String)> {
        self.calls
            .lock()
            .expect("calls")
            .iter()
            .map(|(kind, symbol, _)| (kind.clone(), symbol.clone()))
            .collect()
    }

    /// How many trade fetches were requested for one session date.
    pub fn trade_calls(&self, date: Date) -> usize {
        self.calls
            .lock()
            .expect("calls")
            .iter()
            .filter(|(kind, _, session)| kind == "trade" && *session == Some(date))
            .count()
    }

    /// The highest number of fetches that were in flight at once.
    pub fn max_in_flight(&self) -> u32 {
        *self.max_in_flight.lock().expect("max in flight")
    }

    fn begin(&self, kind: &str, symbols: &[String], start: DateTime) {
        let symbol = symbols.first().cloned().unwrap_or_default();
        self.calls
            .lock()
            .expect("calls")
            .push((kind.to_string(), symbol, Some(start.date())));
        let mut in_flight = self.in_flight.lock().expect("in flight");
        *in_flight += 1;
        let mut max = self.max_in_flight.lock().expect("max in flight");
        *max = (*max).max(*in_flight);
    }

    fn end(&self) {
        let mut in_flight = self.in_flight.lock().expect("in flight");
        *in_flight = in_flight.saturating_sub(1);
    }

    fn failure(&self, kind: &str, date: Date) -> Option<ProviderError> {
        if kind != "trade" {
            return None;
        }
        if self.poisoned.contains(&date) {
            return Some(ProviderError::Configuration(
                self.poison_message
                    .clone()
                    .unwrap_or_else(|| "poisoned session".to_string()),
            ));
        }
        let mut blips = self.blips.lock().expect("blips");
        if let Some(remaining) = blips.get_mut(&date) {
            if *remaining > 0 {
                *remaining -= 1;
                return Some(ProviderError::Transport(
                    "temporary DNS failure".to_string(),
                ));
            }
        }
        None
    }

    async fn pause(&self) {
        if let Some(delay) = self.delay {
            tokio::time::sleep(delay).await;
        }
    }
}

impl EquityDataProvider for FakeAlpaca {
    fn fetch_trades<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        _end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedTrade>, ProviderError>> {
        Box::pin(async move {
            self.begin("trade", symbols, start);
            if let Some(error) = self.failure("trade", start.date()) {
                self.end();
                return Err(error);
            }
            self.pause().await;
            let symbol = symbols.first().cloned().unwrap_or_default();
            self.end();
            Ok(vec![trade(&symbol, start)])
        })
    }

    fn fetch_quotes<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        _end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedQuote>, ProviderError>> {
        Box::pin(async move {
            self.begin("quote", symbols, start);
            self.pause().await;
            let symbol = symbols.first().cloned().unwrap_or_default();
            self.end();
            Ok(vec![quote(&symbol, start)])
        })
    }
}

/// Instrumented crypto provider fake.
#[derive(Default)]
pub struct FakeCrypto {
    calls: Mutex<Vec<(String, String, Option<Date>)>>,
    poisoned: Option<Date>,
    poison_message: Option<String>,
}

impl FakeCrypto {
    /// An instrumented provider that always succeeds.
    pub fn new() -> Self {
        Self::default()
    }

    /// Fail every trade fetch for `date` permanently.
    pub fn poisoning(mut self, date: Date) -> Self {
        self.poisoned = Some(date);
        self.poison_message = Some("poisoned day".to_string());
        self
    }

    /// The `(kind, symbol)` calls observed so far, in order.
    pub fn calls(&self) -> Vec<(String, String)> {
        self.calls
            .lock()
            .expect("calls")
            .iter()
            .map(|(kind, symbol, _)| (kind.clone(), symbol.clone()))
            .collect()
    }

    fn begin(&self, kind: &str, pairs: &[String], start: DateTime) {
        let pair = pairs.first().cloned().unwrap_or_default();
        self.calls
            .lock()
            .expect("calls")
            .push((kind.to_string(), pair, Some(start.date())));
    }

    fn failure(&self, kind: &str, date: Date) -> Option<ProviderError> {
        if kind == "trade" && self.poisoned == Some(date) {
            return Some(ProviderError::Configuration(
                self.poison_message
                    .clone()
                    .unwrap_or_else(|| "poisoned day".to_string()),
            ));
        }
        None
    }
}

impl CryptoDataProvider for FakeCrypto {
    fn fetch_crypto_trades<'a>(
        &'a self,
        pairs: &'a [String],
        start: DateTime,
        _end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedCryptoTrade>, ProviderError>> {
        Box::pin(async move {
            self.begin("trade", pairs, start);
            if let Some(error) = self.failure("trade", start.date()) {
                return Err(error);
            }
            let pair = pairs.first().cloned().unwrap_or_default();
            Ok(vec![crypto_trade(&pair, start)])
        })
    }

    fn fetch_crypto_quotes<'a>(
        &'a self,
        pairs: &'a [String],
        start: DateTime,
        _end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedCryptoQuote>, ProviderError>> {
        Box::pin(async move {
            self.begin("quote", pairs, start);
            let pair = pairs.first().cloned().unwrap_or_default();
            Ok(vec![crypto_quote(&pair, start)])
        })
    }
}

/// One valid equity trade a minute into the session.
pub fn trade(symbol: &str, start: DateTime) -> NormalizedTrade {
    NormalizedTrade {
        timestamp: DateTime::from_micros(start.micros() + 60_000_000),
        symbol: symbol.to_string(),
        trade_id: 1,
        exchange: "XNAS".to_string(),
        price: Decimal18::parse("190.12").expect("price"),
        size: 10,
        conditions: vec!["@".to_string()],
        tape: "C".to_string(),
        update: None,
    }
}

/// One valid equity quote a minute into the session.
pub fn quote(symbol: &str, start: DateTime) -> NormalizedQuote {
    NormalizedQuote {
        timestamp: DateTime::from_micros(start.micros() + 60_000_000),
        symbol: symbol.to_string(),
        bid_exchange: "XNAS".to_string(),
        bid_price: Decimal18::parse("190.11").expect("bid"),
        bid_size: 10,
        ask_exchange: "XNAS".to_string(),
        ask_price: Decimal18::parse("190.13").expect("ask"),
        ask_size: 20,
        conditions: Vec::new(),
        tape: "C".to_string(),
    }
}

/// One valid crypto trade a minute into the day.
pub fn crypto_trade(pair: &str, start: DateTime) -> NormalizedCryptoTrade {
    NormalizedCryptoTrade {
        timestamp: DateTime::from_micros(start.micros() + 60_000_000),
        symbol: pair.to_string(),
        trade_id: Some(1),
        exchange: Some("CBSE".to_string()),
        price: Decimal18::parse("42000.5").expect("price"),
        size: Decimal18::parse("0.5").expect("size"),
        taker_side: Some("B".to_string()),
    }
}

/// One valid crypto quote a minute into the day.
pub fn crypto_quote(pair: &str, start: DateTime) -> NormalizedCryptoQuote {
    NormalizedCryptoQuote {
        timestamp: DateTime::from_micros(start.micros() + 60_000_000),
        symbol: pair.to_string(),
        bid_exchange: Some("CBSE".to_string()),
        bid_price: Decimal18::parse("42000.25").expect("bid"),
        bid_size: Decimal18::parse("0.5").expect("bid size"),
        ask_exchange: Some("CBSE".to_string()),
        ask_price: Decimal18::parse("42001").expect("ask"),
        ask_size: Decimal18::parse("0.5").expect("ask size"),
    }
}
