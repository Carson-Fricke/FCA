//! Point-in-time historical market-data view with hard look-ahead clipping.
//!
//! Port of `harness.data.history`. The view retains quote and trade events in
//! bounded per-subscription ring buffers and answers point-in-time queries
//! relative to an injected engine clock. Every query is clipped to the clock
//! head: asking for data newer than the head returns a look-ahead error
//! instead of silently leaking the future. Timestamps compare on the integer
//! nanosecond timeline so sub-microsecond boundaries survive the query path.

use std::collections::HashMap;

use harness_num::Decimal18;

use crate::bars::{BarAggregator, OhlcvBar};
use crate::clock::Clock;
use crate::error::DataError;
use crate::events::{cutoff_key, end_key, start_key, EventType, MarketEvent, OrderingKey};
use crate::symbol::canonical_symbol;
use crate::time::{DateTime, Nanos};

/// Default ring-buffer capacity for new subscriptions.
pub const DEFAULT_HISTORY_CAPACITY: usize = 4096;

/// The history kinds retained when no selector is given.
const HISTORY_KINDS: [EventType; 2] = [EventType::Quote, EventType::Trade];

/// Which market-event kinds a subscription retains.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HistorySelector {
    /// Retain quotes and trades.
    All,
    /// Retain quotes only.
    Quote,
    /// Retain trades only.
    Trade,
}

impl HistorySelector {
    /// The kinds selected.
    pub fn kinds(self) -> Vec<EventType> {
        match self {
            Self::All => HISTORY_KINDS.to_vec(),
            Self::Quote => vec![EventType::Quote],
            Self::Trade => vec![EventType::Trade],
        }
    }
}

/// One bounded ring-buffer subscription for a symbol and event selector.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HistorySubscription {
    /// Canonical symbol.
    pub symbol: String,
    /// Retained event kinds.
    pub selector: HistorySelector,
    /// Ring-buffer capacity.
    pub capacity: usize,
}

impl HistorySubscription {
    /// Build a validated subscription.
    pub fn new(
        symbol: &str,
        selector: HistorySelector,
        capacity: usize,
    ) -> Result<Self, DataError> {
        if capacity == 0 {
            return Err(DataError::value("capacity must be a positive integer"));
        }
        Ok(Self {
            symbol: canonical_symbol(symbol)?,
            selector,
            capacity,
        })
    }

    /// The history kinds retained by this subscription.
    pub fn kinds(&self) -> Vec<EventType> {
        self.selector.kinds()
    }
}

/// Exact point-in-time statistics for one half-open history window.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HistoryAggregate {
    /// Canonical symbol.
    pub symbol: String,
    /// Inclusive window start.
    pub start: DateTime,
    /// Exclusive window end.
    pub end: DateTime,
    /// Number of retained quotes in the window.
    pub quote_count: usize,
    /// Number of retained trades in the window.
    pub trade_count: usize,
    /// Summed trade quantity.
    pub volume: Decimal18,
    /// Summed trade notional.
    pub notional: Decimal18,
    /// Volume-weighted average price, if volume is non-zero.
    pub vwap: Option<Decimal18>,
    /// Highest trade price.
    pub high: Option<Decimal18>,
    /// Lowest trade price.
    pub low: Option<Decimal18>,
    /// First trade price.
    pub first_price: Option<Decimal18>,
    /// Last trade price.
    pub last_price: Option<Decimal18>,
    /// Last quote bid.
    pub last_bid: Option<Decimal18>,
    /// Last quote ask.
    pub last_ask: Option<Decimal18>,
}

impl HistoryAggregate {
    /// Validate and build an aggregate exactly like Python's `__post_init__`.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        symbol: &str,
        start: DateTime,
        end: DateTime,
        quote_count: usize,
        trade_count: usize,
        volume: Decimal18,
        notional: Decimal18,
        vwap: Option<Decimal18>,
        high: Option<Decimal18>,
        low: Option<Decimal18>,
        first_price: Option<Decimal18>,
        last_price: Option<Decimal18>,
        last_bid: Option<Decimal18>,
        last_ask: Option<Decimal18>,
    ) -> Result<Self, DataError> {
        if end <= start {
            return Err(DataError::value("end must be after start"));
        }
        if volume.is_negative() || notional.is_negative() {
            return Err(DataError::value(
                "volume and notional must be finite, non-negative Decimals",
            ));
        }
        if let (Some(high), Some(low)) = (high, low) {
            if high < low {
                return Err(DataError::value("high must not be below low"));
            }
        }
        Ok(Self {
            symbol: canonical_symbol(symbol)?,
            start,
            end,
            quote_count,
            trade_count,
            volume,
            notional,
            vwap,
            high,
            low,
            first_price,
            last_price,
            last_bid,
            last_ask,
        })
    }
}

/// A fixed-capacity ring buffer of ordered market events with O(1) reads.
#[derive(Debug)]
struct RingBuffer {
    capacity: usize,
    items: Vec<Option<MarketEvent>>,
    start: usize,
    size: usize,
    dropped: u64,
}

impl RingBuffer {
    fn new(capacity: usize) -> Self {
        Self {
            capacity,
            items: (0..capacity).map(|_| None).collect(),
            start: 0,
            size: 0,
            dropped: 0,
        }
    }

    fn len(&self) -> usize {
        self.size
    }

    fn dropped(&self) -> u64 {
        self.dropped
    }

    fn get(&self, index: usize) -> &MarketEvent {
        self.items[(self.start + index) % self.capacity]
            .as_ref()
            .expect("ring slot within size is populated")
    }

    fn append(&mut self, event: MarketEvent) -> Result<(), DataError> {
        if self.size > 0 && event.ordering_key() < self.get(self.size - 1).ordering_key() {
            return Err(DataError::value(
                "event ordering key cannot precede the retained history",
            ));
        }
        let index = (self.start + self.size) % self.capacity;
        self.items[index] = Some(event);
        if self.size == self.capacity {
            self.start = (self.start + 1) % self.capacity;
            self.dropped += 1;
        } else {
            self.size += 1;
        }
        Ok(())
    }

    /// Count of retained events whose ordering key is at or before `cutoff`.
    fn upper_bound(&self, cutoff: OrderingKey) -> usize {
        let mut low = 0usize;
        let mut high = self.size;
        while low < high {
            let mid = (low + high) / 2;
            if self.get(mid).ordering_key() <= cutoff {
                low = mid + 1;
            } else {
                high = mid;
            }
        }
        low
    }

    /// Count of retained events whose ordering key is strictly before `bound`.
    fn lower_bound(&self, bound: OrderingKey) -> usize {
        let mut low = 0usize;
        let mut high = self.size;
        while low < high {
            let mid = (low + high) / 2;
            if self.get(mid).ordering_key() < bound {
                low = mid + 1;
            } else {
                high = mid;
            }
        }
        low
    }

    fn latest(&self, count: usize, cutoff: OrderingKey) -> Vec<MarketEvent> {
        let high = self.upper_bound(cutoff);
        if count == 0 || high == 0 {
            return Vec::new();
        }
        let low = high.saturating_sub(count);
        (low..high).map(|index| self.get(index).clone()).collect()
    }

    fn at(&self, cutoff: OrderingKey) -> Option<&MarketEvent> {
        let index = self.upper_bound(cutoff);
        if index == 0 {
            None
        } else {
            Some(self.get(index - 1))
        }
    }

    fn between(&self, start: OrderingKey, end: OrderingKey) -> Vec<MarketEvent> {
        let low = self.lower_bound(start);
        let high = self.upper_bound(end);
        (low..high).map(|index| self.get(index).clone()).collect()
    }
}

/// Bounded, point-in-time market-data view clipped to the engine clock.
pub struct HistoricalView {
    clock: Box<dyn Clock>,
    capacity: usize,
    buffers: HashMap<(String, EventType), RingBuffer>,
    subscriptions: Vec<HistorySubscription>,
}

impl HistoricalView {
    /// Build a view on `clock` with the default capacity.
    pub fn new(clock: Box<dyn Clock>) -> Self {
        Self::with_capacity(clock, DEFAULT_HISTORY_CAPACITY).expect("default capacity is valid")
    }

    /// Build a view on `clock` with an explicit default capacity.
    pub fn with_capacity(clock: Box<dyn Clock>, capacity: usize) -> Result<Self, DataError> {
        if capacity == 0 {
            return Err(DataError::value("capacity must be a positive integer"));
        }
        Ok(Self {
            clock,
            capacity,
            buffers: HashMap::new(),
            subscriptions: Vec::new(),
        })
    }

    /// The current engine clock head.
    pub fn head(&self) -> DateTime {
        self.clock.now()
    }

    /// The default ring-buffer capacity for new subscriptions.
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// Subscriptions in registration order.
    pub fn subscriptions(&self) -> &[HistorySubscription] {
        &self.subscriptions
    }

    /// Subscribed symbols in stable lexical order.
    pub fn symbols(&self) -> Vec<String> {
        let mut symbols: Vec<String> = self
            .subscriptions
            .iter()
            .map(|subscription| subscription.symbol.clone())
            .collect();
        symbols.sort();
        symbols.dedup();
        symbols
    }

    /// Create bounded buffers for one symbol and selector.
    pub fn subscribe(
        &mut self,
        symbol: &str,
        selector: HistorySelector,
        capacity: Option<usize>,
    ) -> Result<HistorySubscription, DataError> {
        let subscription =
            HistorySubscription::new(symbol, selector, capacity.unwrap_or(self.capacity))?;
        let existing = self.subscriptions.iter().position(|candidate| {
            candidate.symbol == subscription.symbol && candidate.selector == subscription.selector
        });
        if let Some(index) = existing {
            if self.subscriptions[index].capacity >= subscription.capacity {
                return Ok(self.subscriptions[index].clone());
            }
            self.subscriptions.remove(index);
        }
        self.add(subscription.clone())?;
        Ok(subscription)
    }

    fn add(&mut self, subscription: HistorySubscription) -> Result<(), DataError> {
        for kind in subscription.kinds() {
            let key = (subscription.symbol.clone(), kind);
            let needs_replacement = match self.buffers.get(&key) {
                Some(buffer) => buffer.capacity < subscription.capacity,
                None => true,
            };
            if !needs_replacement {
                continue;
            }
            let retained = self
                .buffers
                .get(&key)
                .map(|buffer| buffer.latest(buffer.len(), cutoff_key(Nanos::MAX)))
                .unwrap_or_default();
            let mut replacement = RingBuffer::new(subscription.capacity);
            for event in retained {
                replacement.append(event)?;
            }
            self.buffers.insert(key, replacement);
        }
        self.subscriptions.push(subscription);
        Ok(())
    }

    /// Retain one quote or trade event, rejecting future-dated events.
    pub fn record(&mut self, event: MarketEvent) -> Result<(), DataError> {
        self.guard(&event)?;
        let symbol = canonical_symbol(event.symbol())?;
        let key = (symbol, event.event_type());
        if let Some(buffer) = self.buffers.get_mut(&key) {
            buffer.append(event)?;
        }
        Ok(())
    }

    /// Retain events in iteration order.
    pub fn record_many<I: IntoIterator<Item = MarketEvent>>(
        &mut self,
        events: I,
    ) -> Result<(), DataError> {
        for event in events {
            self.record(event)?;
        }
        Ok(())
    }

    /// How many events are currently retained for a subscription.
    pub fn retained(&self, symbol: &str, selector: HistorySelector) -> Result<usize, DataError> {
        let symbol = canonical_symbol(symbol)?;
        Ok(self
            .keys(&symbol, selector)
            .into_iter()
            .filter_map(|key| self.buffers.get(&key))
            .map(RingBuffer::len)
            .sum())
    }

    /// How many events a subscription has evicted.
    pub fn dropped(&self, symbol: &str, selector: HistorySelector) -> Result<u64, DataError> {
        let symbol = canonical_symbol(symbol)?;
        Ok(self
            .keys(&symbol, selector)
            .into_iter()
            .filter_map(|key| self.buffers.get(&key))
            .map(RingBuffer::dropped)
            .sum())
    }

    /// The newest quote at or before `as_of`.
    pub fn quote(
        &self,
        symbol: &str,
        as_of: Option<DateTime>,
    ) -> Result<Option<MarketEvent>, DataError> {
        self.at_of(symbol, EventType::Quote, as_of)
    }

    /// The newest trade at or before `as_of`.
    pub fn trade(
        &self,
        symbol: &str,
        as_of: Option<DateTime>,
    ) -> Result<Option<MarketEvent>, DataError> {
        self.at_of(symbol, EventType::Trade, as_of)
    }

    fn at_of(
        &self,
        symbol: &str,
        kind: EventType,
        as_of: Option<DateTime>,
    ) -> Result<Option<MarketEvent>, DataError> {
        let cutoff = self.clip(as_of)?;
        let symbol = canonical_symbol(symbol)?;
        Ok(self
            .buffers
            .get(&(symbol, kind))
            .and_then(|buffer| buffer.at(cutoff_key(cutoff)))
            .cloned())
    }

    /// Up to `count` newest matching events at or before `as_of`.
    pub fn last(
        &self,
        symbol: &str,
        count: usize,
        selector: HistorySelector,
        as_of: Option<DateTime>,
    ) -> Result<Vec<MarketEvent>, DataError> {
        let cutoff = self.clip(as_of)?;
        self.merge(&self.buffers_for(symbol, selector)?, count, cutoff)
    }

    /// Up to `count` newest quotes at or before `as_of`.
    pub fn last_quotes(
        &self,
        symbol: &str,
        count: usize,
        as_of: Option<DateTime>,
    ) -> Result<Vec<MarketEvent>, DataError> {
        self.last(symbol, count, HistorySelector::Quote, as_of)
    }

    /// Up to `count` newest trades at or before `as_of`.
    pub fn last_trades(
        &self,
        symbol: &str,
        count: usize,
        as_of: Option<DateTime>,
    ) -> Result<Vec<MarketEvent>, DataError> {
        self.last(symbol, count, HistorySelector::Trade, as_of)
    }

    fn merge(
        &self,
        buffers: &[&RingBuffer],
        count: usize,
        cutoff: Nanos,
    ) -> Result<Vec<MarketEvent>, DataError> {
        if buffers.is_empty() {
            return Ok(Vec::new());
        }
        if buffers.len() == 1 {
            return Ok(buffers[0].latest(count, cutoff_key(cutoff)));
        }
        let cutoff = cutoff_key(cutoff);
        let mut candidates: Vec<MarketEvent> = buffers
            .iter()
            .flat_map(|buffer| buffer.latest(count, cutoff))
            .collect();
        candidates.sort_by_key(MarketEvent::ordering_key);
        if count < candidates.len() {
            Ok(candidates.split_off(candidates.len() - count))
        } else {
            Ok(candidates)
        }
    }

    /// Matching events in `[start, end)`, clipped to the clock.
    pub fn window(
        &self,
        symbol: &str,
        start: DateTime,
        end: DateTime,
        selector: HistorySelector,
        as_of: Option<DateTime>,
    ) -> Result<Vec<MarketEvent>, DataError> {
        let (start, end) = self.window_bounds(start, end, as_of)?;
        let symbol = canonical_symbol(symbol)?;
        let mut events = Vec::new();
        for key in self.keys(&symbol, selector) {
            if let Some(buffer) = self.buffers.get(&key) {
                events.extend(buffer.between(start_key(start), end_key(end)));
            }
        }
        events.sort_by_key(MarketEvent::ordering_key);
        Ok(events)
    }

    /// Quotes in `[start, end)`, clipped to the clock.
    pub fn quotes(
        &self,
        symbol: &str,
        start: DateTime,
        end: DateTime,
        as_of: Option<DateTime>,
    ) -> Result<Vec<MarketEvent>, DataError> {
        self.window(symbol, start, end, HistorySelector::Quote, as_of)
    }

    /// Trades in `[start, end)`, clipped to the clock.
    pub fn trades(
        &self,
        symbol: &str,
        start: DateTime,
        end: DateTime,
        as_of: Option<DateTime>,
    ) -> Result<Vec<MarketEvent>, DataError> {
        self.window(symbol, start, end, HistorySelector::Trade, as_of)
    }

    /// Exact trade and quote statistics for `[start, end)`.
    pub fn aggregate(
        &self,
        symbol: &str,
        start: DateTime,
        end: DateTime,
        as_of: Option<DateTime>,
    ) -> Result<HistoryAggregate, DataError> {
        let (start_ns, end_ns) = self.window_bounds(start, end, as_of)?;
        let symbol_key = canonical_symbol(symbol)?;
        let start = DateTime::from_nanos(start_ns);
        let end = DateTime::from_nanos(end_ns);
        let window_trades = self.trades(&symbol_key, start, end, Some(end))?;
        let window_quotes = self.quotes(&symbol_key, start, end, Some(end))?;
        let mut notional = Decimal18::ZERO;
        let mut volume = Decimal18::ZERO;
        let mut high: Option<Decimal18> = None;
        let mut low: Option<Decimal18> = None;
        let mut first_price: Option<Decimal18> = None;
        let mut last_price: Option<Decimal18> = None;
        for event in &window_trades {
            if let MarketEvent::Trade(trade) = event {
                notional = notional.checked_add(trade.price.checked_mul(trade.quantity)?)?;
                volume = volume.checked_add(trade.quantity)?;
                high = Some(high.map_or(trade.price, |value| value.max(trade.price)));
                low = Some(low.map_or(trade.price, |value| value.min(trade.price)));
                if first_price.is_none() {
                    first_price = Some(trade.price);
                }
                last_price = Some(trade.price);
            }
        }
        let last_quote = window_quotes.last().and_then(|event| match event {
            MarketEvent::Quote(quote) => Some(quote),
            MarketEvent::Trade(_) => None,
        });
        let vwap = if volume.is_zero() {
            None
        } else {
            Some(notional.checked_div(volume)?)
        };
        HistoryAggregate::new(
            &symbol_key,
            start,
            end,
            window_quotes.len(),
            window_trades.len(),
            volume,
            notional,
            vwap,
            high,
            low,
            first_price,
            last_price,
            last_quote.map(|quote| quote.bid_price),
            last_quote.map(|quote| quote.ask_price),
        )
    }

    /// Exact OHLCV bars over retained trades in a window.
    pub fn bars(
        &self,
        symbol: &str,
        interval: &str,
        start: Option<DateTime>,
        end: Option<DateTime>,
        as_of: Option<DateTime>,
    ) -> Result<Vec<OhlcvBar>, DataError> {
        let cutoff = self.clip(as_of)?;
        let start_ns = match start {
            None => 0,
            Some(value) => self.clip(Some(value))?,
        };
        let end_ns = match end {
            None => cutoff,
            Some(value) => self.clip(Some(value))?,
        };
        if end_ns < start_ns {
            return Err(DataError::value("end must not be before start"));
        }
        let mut aggregator = BarAggregator::new(interval, Some(symbol))?;
        for event in self.trades(
            symbol,
            DateTime::from_nanos(start_ns),
            DateTime::from_nanos(end_ns),
            Some(DateTime::from_nanos(cutoff)),
        )? {
            if let MarketEvent::Trade(trade) = event {
                aggregator.add(trade)?;
            }
        }
        aggregator.finalize()
    }

    /// Read the newest matching event no later than `timestamp`.
    pub fn read(
        &self,
        timestamp: DateTime,
        symbol: &str,
        selector: HistorySelector,
    ) -> Result<Option<MarketEvent>, DataError> {
        let cutoff = self.clip(Some(timestamp))?;
        let symbol = canonical_symbol(symbol)?;
        let mut best: Option<MarketEvent> = None;
        let mut best_key: Option<OrderingKey> = None;
        for key in self.keys(&symbol, selector) {
            let Some(buffer) = self.buffers.get(&key) else {
                continue;
            };
            let Some(candidate) = buffer.at(cutoff_key(cutoff)) else {
                continue;
            };
            let candidate_key = candidate.ordering_key();
            let better = match best_key {
                None => true,
                Some(key) => candidate_key > key,
            };
            if better {
                best = Some(candidate.clone());
                best_key = Some(candidate_key);
            }
        }
        Ok(best)
    }

    fn guard(&self, event: &MarketEvent) -> Result<(), DataError> {
        let head = self.clock.now().to_nanos();
        let stamp = event.timestamp().to_nanos();
        if stamp > head {
            return Err(DataError::Lookahead(format!(
                "event {:?} at {stamp} is newer than the engine clock head {head}",
                event.event_id()
            )));
        }
        Ok(())
    }

    fn clip(&self, value: Option<DateTime>) -> Result<Nanos, DataError> {
        let head = self.clock.now().to_nanos();
        let requested = match value {
            None => head,
            Some(value) => value.to_nanos(),
        };
        if requested > head {
            return Err(DataError::Lookahead(format!(
                "{requested} is newer than the engine clock head {head}"
            )));
        }
        Ok(requested)
    }

    fn keys(&self, symbol: &str, selector: HistorySelector) -> Vec<(String, EventType)> {
        selector
            .kinds()
            .into_iter()
            .map(|kind| (symbol.to_string(), kind))
            .collect()
    }

    fn buffers_for(
        &self,
        symbol: &str,
        selector: HistorySelector,
    ) -> Result<Vec<&RingBuffer>, DataError> {
        let symbol = canonical_symbol(symbol)?;
        Ok(self
            .keys(&symbol, selector)
            .into_iter()
            .filter_map(|key| self.buffers.get(&key))
            .collect())
    }

    fn window_bounds(
        &self,
        start: DateTime,
        end: DateTime,
        as_of: Option<DateTime>,
    ) -> Result<(Nanos, Nanos), DataError> {
        let start_ns = self.clip(Some(start))?;
        let end_ns = self.clip(Some(end))?;
        self.clip(as_of)?;
        if end_ns < start_ns {
            return Err(DataError::value("end must not be before start"));
        }
        Ok((start_ns, end_ns))
    }
}

/// Opaque, now-anchored point-in-time data capability for strategies.
pub struct StrategyHistory<'a> {
    view: &'a HistoricalView,
}

impl<'a> StrategyHistory<'a> {
    /// Wrap an engine-owned view.
    pub fn new(view: &'a HistoricalView) -> Self {
        Self { view }
    }

    /// Read the newest matching event at or before the engine clock head.
    pub fn read(
        &self,
        symbol: &str,
        selector: HistorySelector,
    ) -> Result<Option<MarketEvent>, DataError> {
        self.view.read(self.view.head(), symbol, selector)
    }

    /// Up to `count` newest matching events at the engine head.
    pub fn last(
        &self,
        symbol: &str,
        count: usize,
        selector: HistorySelector,
    ) -> Result<Vec<MarketEvent>, DataError> {
        self.view
            .last(symbol, count, selector, Some(self.view.head()))
    }

    /// Exact statistics capped at the head.
    pub fn aggregate(
        &self,
        symbol: &str,
        start: DateTime,
        end: Option<DateTime>,
    ) -> Result<HistoryAggregate, DataError> {
        self.view
            .aggregate(symbol, start, end.unwrap_or_else(|| self.view.head()), None)
    }

    /// Exact OHLCV bars capped at the head.
    pub fn bars(
        &self,
        symbol: &str,
        interval: &str,
        start: Option<DateTime>,
        end: Option<DateTime>,
    ) -> Result<Vec<OhlcvBar>, DataError> {
        self.view.bars(symbol, interval, start, end, None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clock::ManualClock;
    use crate::events::{QuoteEvent, TradeEvent};
    use harness_num::Decimal18;
    use std::str::FromStr;

    fn dec(value: &str) -> Decimal18 {
        Decimal18::from_str(value).unwrap()
    }

    fn instant(seconds: i64) -> DateTime {
        DateTime::from_micros(seconds * 1_000_000)
    }

    fn trade(symbol: &str, seconds: i64, sequence: u64, price: &str) -> MarketEvent {
        MarketEvent::Trade(TradeEvent::new(
            symbol.to_string(),
            instant(seconds),
            sequence,
            dec(price),
            dec("1"),
        ))
    }

    fn quote(symbol: &str, seconds: i64, sequence: u64, bid: &str, ask: &str) -> MarketEvent {
        MarketEvent::Quote(QuoteEvent::new(
            symbol.to_string(),
            instant(seconds),
            sequence,
            dec(bid),
            dec(ask),
        ))
    }

    fn view() -> (ManualClock, HistoricalView) {
        let clock = ManualClock::new(instant(100));
        let mut view = HistoricalView::new(Box::new(ManualClock::new(instant(100))));
        view.subscribe("AAPL", HistorySelector::All, None).unwrap();
        (clock, view)
    }

    #[test]
    fn reads_are_clipped_to_head() {
        let (_clock, mut view) = view();
        view.record(trade("AAPL", 10, 1, "10")).unwrap();
        let err = view.quote("AAPL", Some(instant(200))).unwrap_err();
        assert!(matches!(err, DataError::Lookahead(_)));
    }

    #[test]
    fn latest_and_window_match_python_order() {
        let (_clock, mut view) = view();
        view.record(quote("AAPL", 1, 1, "9.5", "10.5")).unwrap();
        view.record(trade("AAPL", 1, 2, "10")).unwrap();
        view.record(trade("AAPL", 2, 3, "11")).unwrap();
        let last = view.last("AAPL", 2, HistorySelector::All, None).unwrap();
        assert_eq!(last.len(), 2);
        assert_eq!(last[0].event_id(), "AAPL-2");
        assert_eq!(last[1].event_id(), "AAPL-3");
        let window = view
            .window("AAPL", instant(1), instant(2), HistorySelector::All, None)
            .unwrap();
        assert_eq!(window.len(), 2);
    }

    #[test]
    fn aggregate_computes_vwap() {
        let (_clock, mut view) = view();
        view.record(trade("AAPL", 1, 1, "10")).unwrap();
        view.record(trade("AAPL", 2, 2, "20")).unwrap();
        let aggregate = view
            .aggregate("AAPL", instant(0), instant(10), None)
            .unwrap();
        assert_eq!(aggregate.trade_count, 2);
        assert_eq!(aggregate.volume, dec("2"));
        assert_eq!(aggregate.notional, dec("30"));
        assert_eq!(aggregate.vwap, Some(dec("15")));
        assert_eq!(aggregate.high, Some(dec("20")));
        assert_eq!(aggregate.low, Some(dec("10")));
    }

    #[test]
    fn ring_buffer_evicts_oldest() {
        let (_clock, mut view) = view();
        view.subscribe("MSFT", HistorySelector::Trade, Some(2))
            .unwrap();
        for seconds in 1..=3 {
            view.record(trade("MSFT", seconds, seconds as u64, "10"))
                .unwrap();
        }
        assert_eq!(view.retained("MSFT", HistorySelector::Trade).unwrap(), 2);
        assert_eq!(view.dropped("MSFT", HistorySelector::Trade).unwrap(), 1);
    }
}
