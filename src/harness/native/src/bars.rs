//! Exact OHLCV bar derivation from immutable trade events, mirroring
//! `src/harness/data/bars.py`.
//!
//! Prices and volume remain the pinned `i128` fixed-point type all the way
//! through aggregation so every bar reconciles exactly to its source trades.
//! Buckets are half-open `[start, end)` UTC intervals floored from the Unix
//! epoch; bars are ordered by interval start, and the trades inside one bar are
//! ordered by `(timestamp, sequence, feed index)` exactly like Python's stable
//! sort in `_make_bar`.

use crate::decimal;
use crate::market::TradeEvent;

/// A checked failure from bar derivation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// Allocation failure.
    OutOfMemory,
    /// A required argument was invalid.
    InvalidArgument,
    /// The symbol was invalid.
    InvalidSymbol,
    /// A decimal operation failed.
    InvalidDecimal,
    /// A value was out of range.
    OutOfRange,
}

impl core::fmt::Display for Error {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        let message = match self {
            Self::OutOfMemory => "out of memory",
            Self::InvalidArgument => "invalid argument",
            Self::InvalidSymbol => "invalid symbol",
            Self::InvalidDecimal => "invalid decimal",
            Self::OutOfRange => "out of range",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for Error {}

impl From<crate::market::Error> for Error {
    fn from(error: crate::market::Error) -> Self {
        match error {
            crate::market::Error::OutOfMemory => Self::OutOfMemory,
            crate::market::Error::InvalidArgument => Self::InvalidArgument,
            crate::market::Error::InvalidSymbol => Self::InvalidSymbol,
            crate::market::Error::InvalidDecimal => Self::InvalidDecimal,
            crate::market::Error::OutOfRange => Self::OutOfRange,
        }
    }
}

/// One non-empty, half-open OHLCV interval derived from trade events.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OHLCVBar<'a> {
    /// Canonical symbol.
    pub symbol: &'a str,
    /// Inclusive interval start (epoch nanoseconds).
    pub start_ns: i128,
    /// Exclusive interval end (epoch nanoseconds).
    pub end_ns: i128,
    /// First trade price.
    pub open: i128,
    /// Highest trade price.
    pub high: i128,
    /// Lowest trade price.
    pub low: i128,
    /// Last trade price.
    pub close: i128,
    /// Summed traded quantity.
    pub volume: i128,
    /// Number of trades in the interval.
    pub trade_count: usize,
}

impl OHLCVBar<'_> {
    /// Validate the bar against the Python record contract.
    pub fn validate(&self) -> Result<(), Error> {
        if self.end_ns <= self.start_ns {
            return Err(Error::InvalidArgument);
        }
        if self.volume < 0 {
            return Err(Error::InvalidArgument);
        }
        if self.high < self.low
            || self.open < self.low
            || self.open > self.high
            || self.close < self.low
            || self.close > self.high
        {
            return Err(Error::InvalidArgument);
        }
        if self.trade_count == 0 {
            return Err(Error::InvalidArgument);
        }
        Ok(())
    }

    /// The bar start under the conventional OHLCV name.
    pub const fn timestamp_ns(&self) -> i128 {
        self.start_ns
    }
}

const NS_PER_SECOND: i128 = 1_000_000_000;

/// Normalize a configured bar interval to positive nanoseconds. Strings use the
/// harness configuration vocabulary: `s`, `m`/`min`, `h`, and `d`.
pub fn interval_delta_ns(interval: &str) -> Result<i128, Error> {
    if interval.is_empty() || interval.len() > 32 {
        return Err(Error::InvalidArgument);
    }
    let value = interval.to_ascii_lowercase();
    let value = value.trim_matches(|character: char| matches!(character, ' ' | '\t' | '\r' | '\n'));
    let digits = value.bytes().take_while(u8::is_ascii_digit).count();
    if digits == 0 {
        return Err(Error::InvalidArgument);
    }
    let unit = &value[digits..];
    let seconds: i128 = if unit == "s" {
        1
    } else if unit == "m" || unit == "min" {
        60
    } else if unit == "h" {
        3600
    } else if unit == "d" {
        86_400
    } else {
        return Err(Error::InvalidArgument);
    };
    let amount: i128 = value[..digits]
        .parse()
        .map_err(|_| Error::InvalidArgument)?;
    if amount <= 0 {
        return Err(Error::InvalidArgument);
    }
    amount
        .checked_mul(seconds * NS_PER_SECOND)
        .ok_or(Error::OutOfRange)
}

/// Floor `value_ns` into its half-open bucket start (the Unix epoch is the
/// origin).
pub fn bucket_start_ns(value_ns: i128, interval_ns: i128) -> i128 {
    value_ns.div_euclid(interval_ns) * interval_ns
}

#[derive(Clone, Debug)]
struct Bucket<'a> {
    start_ns: i128,
    trades: Vec<TradeEvent<'a>>,
}

/// Accumulate one symbol's trades into deterministic OHLCV bars.
#[derive(Clone, Debug, Default)]
pub struct BarAggregator<'a> {
    interval_ns: i128,
    symbol: Option<&'a str>,
    buckets: Vec<Bucket<'a>>,
}

impl<'a> BarAggregator<'a> {
    /// Create an aggregator for `interval_ns`.
    pub fn new(interval_ns: i128, symbol: Option<&'a str>) -> Result<Self, Error> {
        if interval_ns <= 0 {
            return Err(Error::InvalidArgument);
        }
        Ok(Self {
            interval_ns,
            symbol,
            buckets: Vec::new(),
        })
    }

    /// The aggregator's symbol (adopted from the first trade when unset).
    pub fn symbol(&self) -> Option<&'a str> {
        self.symbol
    }

    /// Add one trade without consulting any external clock or data source.
    pub fn add(&mut self, trade: TradeEvent<'a>) -> Result<(), Error> {
        trade.validate()?;
        match self.symbol {
            None => self.symbol = Some(trade.symbol),
            Some(symbol) if symbol != trade.symbol => return Err(Error::InvalidArgument),
            Some(_) => {}
        }
        let start = bucket_start_ns(trade.timestamp_ns, self.interval_ns);
        for bucket in &mut self.buckets {
            if bucket.start_ns == start {
                bucket.trades.push(trade);
                return Ok(());
            }
        }
        self.buckets.push(Bucket {
            start_ns: start,
            trades: vec![trade],
        });
        Ok(())
    }

    /// Add trades in source-feed order.
    pub fn add_many(&mut self, trades: &[TradeEvent<'a>]) -> Result<(), Error> {
        for &trade in trades {
            self.add(trade)?;
        }
        Ok(())
    }

    /// Return non-empty bars ordered by UTC interval start.
    pub fn finalize(&mut self) -> Result<Vec<OHLCVBar<'a>>, Error> {
        self.buckets.sort_by_key(|bucket| bucket.start_ns);
        self.buckets
            .iter()
            .map(|bucket| make_bar(bucket.start_ns, self.interval_ns, &bucket.trades))
            .collect()
    }
}

fn make_bar<'a>(
    start_ns: i128,
    interval_ns: i128,
    trades: &[TradeEvent<'a>],
) -> Result<OHLCVBar<'a>, Error> {
    if trades.is_empty() {
        return Err(Error::InvalidArgument);
    }
    let mut indices: Vec<usize> = (0..trades.len()).collect();
    indices.sort_by(|&a, &b| {
        (trades[a].timestamp_ns, trades[a].sequence, a).cmp(&(
            trades[b].timestamp_ns,
            trades[b].sequence,
            b,
        ))
    });

    let mut high = trades[indices[0]].price;
    let mut low = trades[indices[0]].price;
    let mut volume: i128 = 0;
    for &index in &indices {
        let price = trades[index].price;
        if price > high {
            high = price;
        }
        if price < low {
            low = price;
        }
        volume = decimal::add(volume, trades[index].quantity).map_err(|error| match error {
            decimal::DecimalError::OutOfRange => Error::OutOfRange,
            decimal::DecimalError::InvalidDecimal => Error::InvalidDecimal,
        })?;
    }
    let bar = OHLCVBar {
        symbol: trades[indices[0]].symbol,
        start_ns,
        end_ns: start_ns + interval_ns,
        open: trades[indices[0]].price,
        high,
        low,
        close: trades[indices[indices.len() - 1]].price,
        volume,
        trade_count: indices.len(),
    };
    bar.validate()?;
    Ok(bar)
}

/// Build exact bars from a finite trade slice.
pub fn build_bars<'a>(
    trades: &[TradeEvent<'a>],
    interval_ns: i128,
    symbol: Option<&'a str>,
) -> Result<Vec<OHLCVBar<'a>>, Error> {
    let mut aggregator = BarAggregator::new(interval_ns, symbol)?;
    aggregator.add_many(trades)?;
    aggregator.finalize()
}

#[cfg(test)]
#[path = "../tests/unit/bars.rs"]
mod tests;
