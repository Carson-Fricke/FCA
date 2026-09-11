//! Exact OHLCV bar derivation from immutable trade events.
//!
//! Port of `harness.data.bars`. Prices and volume stay fixed-point decimals all
//! the way through aggregation so a bar can be reconciled exactly to its
//! source trades. Buckets are aligned to the Unix epoch exactly like the
//! Python `_bucket_start` floor division.

use std::collections::BTreeMap;

use harness_num::Decimal18;

use crate::error::DataError;
use crate::events::TradeEvent;
use crate::symbol::canonical_symbol;
use crate::time::Nanos;

const NANOS_PER_SECOND: i64 = 1_000_000_000;
const NANOS_PER_MINUTE: i64 = 60 * NANOS_PER_SECOND;
const NANOS_PER_HOUR: i64 = 60 * NANOS_PER_MINUTE;
const NANOS_PER_DAY: i64 = 24 * NANOS_PER_HOUR;

/// Normalize a configured bar interval to a positive nanosecond duration.
///
/// Accepts `s`, `m`/`min`, `h`, and `d`, mirroring Python's `interval_delta`.
pub fn interval_delta(interval: &str) -> Result<Nanos, DataError> {
    let value = interval.trim().to_lowercase();
    let split = value
        .find(|c: char| !c.is_ascii_digit())
        .unwrap_or(value.len());
    let number = &value[..split];
    let unit = &value[split..];
    let invalid = || DataError::value("interval must be a positive timedelta or s/min/h/d value");
    if number.is_empty() || unit.is_empty() {
        return Err(invalid());
    }
    let amount: i64 = number.parse().map_err(|_| invalid())?;
    if amount <= 0 {
        return Err(DataError::value("interval must be positive"));
    }
    let delta = match unit {
        "s" => amount * NANOS_PER_SECOND,
        "m" | "min" => amount * NANOS_PER_MINUTE,
        "h" => amount * NANOS_PER_HOUR,
        "d" => amount * NANOS_PER_DAY,
        _ => return Err(invalid()),
    };
    Ok(delta)
}

/// Normalize an interval expressed as a nanosecond duration.
pub fn interval_delta_nanos(interval: Nanos) -> Result<Nanos, DataError> {
    if interval <= 0 {
        return Err(DataError::value("interval must be positive"));
    }
    Ok(interval)
}

fn bucket_start(nanos: Nanos, interval: Nanos) -> Nanos {
    nanos.div_euclid(interval) * interval
}

/// One non-empty, half-open OHLCV interval derived from trade events.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OhlcvBar {
    /// Canonical symbol.
    pub symbol: String,
    /// Inclusive bar start.
    pub start: crate::time::DateTime,
    /// Exclusive bar end.
    pub end: crate::time::DateTime,
    /// First trade price in the interval.
    pub open: Decimal18,
    /// Highest trade price in the interval.
    pub high: Decimal18,
    /// Lowest trade price in the interval.
    pub low: Decimal18,
    /// Last trade price in the interval.
    pub close: Decimal18,
    /// Summed trade quantity.
    pub volume: Decimal18,
    /// Number of source trades.
    pub trade_count: u32,
}

impl OhlcvBar {
    /// Validate and build one bar exactly like Python's `__post_init__`.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        symbol: &str,
        start: crate::time::DateTime,
        end: crate::time::DateTime,
        open: Decimal18,
        high: Decimal18,
        low: Decimal18,
        close: Decimal18,
        volume: Decimal18,
        trade_count: u32,
    ) -> Result<Self, DataError> {
        if end <= start {
            return Err(DataError::value("end must be after start"));
        }
        if volume.is_negative() {
            return Err(DataError::value("volume must not be negative"));
        }
        if high < low || open < low || open > high || close < low || close > high {
            return Err(DataError::value(
                "OHLC values must reconcile within low and high",
            ));
        }
        if trade_count == 0 {
            return Err(DataError::value("trade_count must be a positive integer"));
        }
        Ok(Self {
            symbol: canonical_symbol(symbol)?,
            start,
            end,
            open,
            high,
            low,
            close,
            volume,
            trade_count,
        })
    }

    /// The inclusive bar start under the conventional OHLCV name.
    pub fn timestamp(&self) -> crate::time::DateTime {
        self.start
    }

    /// The exclusive bar end.
    pub fn close_time(&self) -> crate::time::DateTime {
        self.end
    }

    /// The number of source trades.
    pub fn count(&self) -> u32 {
        self.trade_count
    }
}

/// Descriptive aliases matching the Python surface.
pub type Bar = OhlcvBar;
/// Descriptive aliases matching the Python surface.
pub type Ohlcv = OhlcvBar;

/// Accumulate one symbol's trades into deterministic OHLCV bars.
#[derive(Debug)]
pub struct BarAggregator {
    interval: Nanos,
    symbol: Option<String>,
    buckets: BTreeMap<Nanos, Vec<(u64, TradeEvent)>>,
    next_index: u64,
}

impl BarAggregator {
    /// Build an aggregator for `interval` (a string such as `"1min"`).
    pub fn new(interval: &str, symbol: Option<&str>) -> Result<Self, DataError> {
        Ok(Self::with_nanos(
            interval_delta(interval)?,
            symbol.map(canonical_symbol).transpose()?,
        ))
    }

    /// Build an aggregator from a nanosecond interval.
    pub fn with_nanos(interval: Nanos, symbol: Option<String>) -> Self {
        Self {
            interval,
            symbol,
            buckets: BTreeMap::new(),
            next_index: 0,
        }
    }

    /// The exact configured interval.
    pub fn interval(&self) -> Nanos {
        self.interval
    }

    /// The selected symbol, if configured or first observed.
    pub fn symbol(&self) -> Option<&str> {
        self.symbol.as_deref()
    }

    /// Add one trade.
    pub fn add(&mut self, trade: TradeEvent) -> Result<(), DataError> {
        match &self.symbol {
            None => self.symbol = Some(trade.symbol.clone()),
            Some(symbol) if *symbol != trade.symbol => {
                return Err(DataError::value("all trades must have the same symbol"));
            }
            Some(_) => {}
        }
        let bucket = bucket_start(trade.timestamp.to_nanos(), self.interval);
        let index = self.next_index;
        self.next_index += 1;
        self.buckets.entry(bucket).or_default().push((index, trade));
        Ok(())
    }

    /// Add trades in source-feed order.
    pub fn add_many<I: IntoIterator<Item = TradeEvent>>(
        &mut self,
        trades: I,
    ) -> Result<(), DataError> {
        for trade in trades {
            self.add(trade)?;
        }
        Ok(())
    }

    /// Return non-empty bars ordered by UTC interval start.
    pub fn finalize(&self) -> Result<Vec<OhlcvBar>, DataError> {
        self.buckets
            .iter()
            .map(|(start, trades)| make_bar(*start, self.interval, trades))
            .collect()
    }

    /// Add `trades` and return the resulting bars.
    pub fn aggregate<I: IntoIterator<Item = TradeEvent>>(
        &mut self,
        trades: I,
    ) -> Result<Vec<OhlcvBar>, DataError> {
        self.add_many(trades)?;
        self.finalize()
    }
}

/// Descriptive alias matching the Python surface.
pub type BarBuilder = BarAggregator;

fn make_bar(
    start: Nanos,
    interval: Nanos,
    trades: &[(u64, TradeEvent)],
) -> Result<OhlcvBar, DataError> {
    if trades.is_empty() {
        return Err(DataError::value("cannot build a bar without trades"));
    }
    let mut ordered: Vec<&TradeEvent> = trades.iter().map(|(_, trade)| trade).collect();
    ordered.sort_by_key(|trade| (trade.timestamp, trade.sequence));
    // The stable sort preserves insertion order for identical keys, matching
    // the Python key `(timestamp, sequence, insertion_index)`.
    let first = ordered[0];
    let mut high = first.price;
    let mut low = first.price;
    let mut volume = Decimal18::ZERO;
    let mut trade_count: u32 = 0;
    for trade in &ordered {
        high = high.max(trade.price);
        low = low.min(trade.price);
        volume = volume.checked_add(trade.quantity)?;
        trade_count += 1;
    }
    let close = ordered[ordered.len() - 1].price;
    OhlcvBar::new(
        &first.symbol,
        crate::time::DateTime::from_nanos(start),
        crate::time::DateTime::from_nanos(start + interval),
        first.price,
        high,
        low,
        close,
        volume,
        trade_count,
    )
}

/// Build exact bars from a finite trade iterable.
pub fn build_bars<I: IntoIterator<Item = TradeEvent>>(
    trades: I,
    interval: &str,
    symbol: Option<&str>,
) -> Result<Vec<OhlcvBar>, DataError> {
    let mut aggregator = BarAggregator::new(interval, symbol)?;
    aggregator.aggregate(trades)
}

/// Descriptive alias matching the Python surface.
pub fn aggregate_trades<I: IntoIterator<Item = TradeEvent>>(
    trades: I,
    interval: &str,
    symbol: Option<&str>,
) -> Result<Vec<OhlcvBar>, DataError> {
    build_bars(trades, interval, symbol)
}

/// Descriptive alias matching the Python surface.
pub fn bars_from_trades<I: IntoIterator<Item = TradeEvent>>(
    trades: I,
    interval: &str,
    symbol: Option<&str>,
) -> Result<Vec<OhlcvBar>, DataError> {
    build_bars(trades, interval, symbol)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::time::DateTime;
    use harness_num::Decimal18;
    use std::str::FromStr;

    fn dec(value: &str) -> Decimal18 {
        Decimal18::from_str(value).unwrap()
    }

    fn trade(symbol: &str, seconds: i64, sequence: u64, price: &str, qty: &str) -> TradeEvent {
        TradeEvent::new(
            symbol.to_string(),
            DateTime::from_micros(seconds * 1_000_000),
            sequence,
            dec(price),
            dec(qty),
        )
    }

    #[test]
    fn interval_delta_units() {
        assert_eq!(interval_delta("1s").unwrap(), 1_000_000_000);
        assert_eq!(interval_delta("5min").unwrap(), 300_000_000_000);
        assert_eq!(interval_delta("1h").unwrap(), 3_600_000_000_000);
        assert_eq!(interval_delta("1d").unwrap(), 86_400_000_000_000);
        assert!(interval_delta("0s").is_err());
        assert!(interval_delta("1w").is_err());
        assert!(interval_delta("abc").is_err());
    }

    #[test]
    fn bars_aggregate_exactly() {
        let trades = vec![
            trade("AAPL", 1, 1, "10.00", "2"),
            trade("AAPL", 2, 2, "12.00", "1"),
            trade("AAPL", 61, 3, "9.00", "3"),
        ];
        let bars = build_bars(trades, "1min", Some("AAPL")).unwrap();
        assert_eq!(bars.len(), 2);
        let first = &bars[0];
        assert_eq!(first.open, dec("10.00"));
        assert_eq!(first.high, dec("12.00"));
        assert_eq!(first.low, dec("10.00"));
        assert_eq!(first.close, dec("12.00"));
        assert_eq!(first.volume, dec("3"));
        assert_eq!(first.trade_count, 2);
        assert_eq!(bars[1].trade_count, 1);
        assert_eq!(bars[1].start.to_nanos(), 60_000_000_000);
    }

    #[test]
    fn mismatched_symbol_rejected() {
        let mut aggregator = BarAggregator::new("1min", Some("AAPL")).unwrap();
        aggregator.add(trade("AAPL", 1, 1, "10", "1")).unwrap();
        assert!(aggregator.add(trade("MSFT", 2, 2, "10", "1")).is_err());
    }
}
