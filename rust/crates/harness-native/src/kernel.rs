//! Deterministic event kernel and replay driver.
//!
//! This module hosts the port of `replay.zig`: a K-way deterministic replay
//! merge over one-lookahead source cursors. Events are merged by timestamp,
//! then source sequence, then the serialized event-class priority table
//! (`crate::market::EVENT_CLASS_PRIORITY`, where quotes precede trades at
//! equal keys), then the source feed order, then the source-local row order.
//!
//! The event queue and bus themselves live in [`crate::market_bus`], and the
//! deterministic clock in [`crate::clock`]; together they form the native
//! event kernel.

pub mod replay {
    use crate::market::{EventType, MarketEvent};

    /// A checked failure from the replay feed.
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
        /// An Arrow bridge failure.
        InvalidArrow,
        /// A value was out of range.
        OutOfRange,
        /// Each replay source must be ordered by timestamp and sequence.
        RetrogradeSource,
        /// Replay event symbol does not match its source.
        SymbolMismatch,
        /// Replay event type does not match its source data type.
        EventTypeMismatch,
    }

    impl core::fmt::Display for Error {
        fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
            let message = match self {
                Self::OutOfMemory => "out of memory",
                Self::InvalidArgument => "invalid argument",
                Self::InvalidSymbol => "invalid symbol",
                Self::InvalidDecimal => "invalid decimal",
                Self::InvalidArrow => "invalid arrow batch",
                Self::OutOfRange => "out of range",
                Self::RetrogradeSource => "source is not ordered by timestamp and sequence",
                Self::SymbolMismatch => "event symbol does not match its source",
                Self::EventTypeMismatch => "event type does not match its source",
            };
            formatter.write_str(message)
        }
    }

    impl std::error::Error for Error {}

    const NS_PER_DAY: i128 = 86_400 * 1_000_000_000;

    /// One explicit half-open replay session.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Session {
        /// Session date in days since the Unix epoch.
        pub day: i64,
        /// Inclusive window open (epoch nanoseconds).
        pub open_ns: i128,
        /// Exclusive window close (epoch nanoseconds).
        pub close_ns: i128,
    }

    impl Session {
        /// Create a validated session window.
        pub fn new(day: i64, open_ns: i128, close_ns: i128) -> Result<Self, Error> {
            if close_ns <= open_ns {
                return Err(Error::InvalidArgument);
            }
            Ok(Self {
                day,
                open_ns,
                close_ns,
            })
        }

        /// The half-open UTC day `[00:00, 24:00)` for one date.
        pub fn utc_day(day: i64) -> Self {
            let open = i128::from(day) * NS_PER_DAY;
            Self {
                day,
                open_ns: open,
                close_ns: open + NS_PER_DAY,
            }
        }
    }

    /// The ordering key of one source event.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct SourceKey {
        /// Epoch nanoseconds.
        pub timestamp_ns: i128,
        /// Source sequence number.
        pub sequence: u64,
        /// Event-class priority.
        pub class_priority: u8,
    }

    fn key_less(a: SourceKey, b: SourceKey) -> bool {
        if a.timestamp_ns != b.timestamp_ns {
            return a.timestamp_ns < b.timestamp_ns;
        }
        if a.sequence != b.sequence {
            return a.sequence < b.sequence;
        }
        a.class_priority < b.class_priority
    }

    /// A one-lookahead cursor over one explicitly ordered source.
    #[derive(Clone, Debug)]
    pub struct EventSource<'a> {
        symbol: &'a str,
        expected_type: Option<EventType>,
        session_day: Option<i64>,
        events: &'a [MarketEvent<'a>],
        index: usize,
        last_key: Option<SourceKey>,
        validated_current: bool,
    }

    impl<'a> EventSource<'a> {
        /// Create a source cursor.
        pub fn new(
            symbol: &'a str,
            expected_type: Option<EventType>,
            session_day: Option<i64>,
            events: &'a [MarketEvent<'a>],
        ) -> Result<Self, Error> {
            if symbol.is_empty() {
                return Err(Error::InvalidArgument);
            }
            Ok(Self {
                symbol,
                expected_type,
                session_day,
                events,
                index: 0,
                last_key: None,
                validated_current: false,
            })
        }

        /// The source's canonical symbol.
        pub fn symbol(&self) -> &'a str {
            self.symbol
        }

        /// The source's optional expected event class.
        pub fn expected_type(&self) -> Option<EventType> {
            self.expected_type
        }

        /// The source's optional pinned session day.
        pub fn session_day(&self) -> Option<i64> {
            self.session_day
        }

        /// Return the current lookahead event without consuming it.
        pub fn peek(&mut self) -> Result<Option<MarketEvent<'a>>, Error> {
            if self.index >= self.events.len() {
                return Ok(None);
            }
            if !self.validated_current {
                let event = self.events[self.index];
                let key = SourceKey {
                    timestamp_ns: event.timestamp_ns(),
                    sequence: event.sequence(),
                    class_priority: event.class_priority(),
                };
                if let Some(last) = self.last_key {
                    if key_less(key, last) {
                        return Err(Error::RetrogradeSource);
                    }
                }
                self.last_key = Some(key);
                self.validated_current = true;
            }
            Ok(Some(self.events[self.index]))
        }

        /// Consume and return the current event.
        pub fn pop(&mut self) -> Result<MarketEvent<'a>, Error> {
            let event = self.peek()?.ok_or(Error::InvalidArgument)?;
            self.index += 1;
            self.validated_current = false;
            Ok(event)
        }

        /// The source-local ordinal of the current event.
        pub fn row_order(&self) -> usize {
            self.index
        }
    }

    #[derive(Clone, Copy, Debug)]
    struct HeapItem<'a> {
        key: SourceKey,
        source_order: usize,
        row_order: usize,
        event: MarketEvent<'a>,
    }

    fn item_less(a: HeapItem<'_>, b: HeapItem<'_>) -> bool {
        if key_less(a.key, b.key) {
            return true;
        }
        if key_less(b.key, a.key) {
            return false;
        }
        if a.source_order != b.source_order {
            return a.source_order < b.source_order;
        }
        a.row_order < b.row_order
    }

    fn heap_push<'a>(heap: &mut Vec<HeapItem<'a>>, item: HeapItem<'a>) {
        heap.push(item);
        let mut child = heap.len() - 1;
        while child > 0 {
            let parent = (child - 1) / 2;
            if !item_less(heap[child], heap[parent]) {
                break;
            }
            heap.swap(child, parent);
            child = parent;
        }
    }

    fn heap_pop<'a>(heap: &mut Vec<HeapItem<'a>>) -> HeapItem<'a> {
        let root = heap[0];
        let last = heap.pop().expect("heap is non-empty");
        if !heap.is_empty() {
            heap[0] = last;
            let mut index = 0usize;
            loop {
                let left = 2 * index + 1;
                let right = 2 * index + 2;
                let mut smallest = index;
                if left < heap.len() && item_less(heap[left], heap[smallest]) {
                    smallest = left;
                }
                if right < heap.len() && item_less(heap[right], heap[smallest]) {
                    smallest = right;
                }
                if smallest == index {
                    break;
                }
                heap.swap(index, smallest);
                index = smallest;
            }
        }
        root
    }

    /// Stream deterministic market events one explicit session at a time.
    #[derive(Clone, Debug, Default)]
    pub struct ReplayFeed<'a> {
        sources: Vec<EventSource<'a>>,
        sessions: Vec<Session>,
    }

    impl<'a> ReplayFeed<'a> {
        /// Create an empty feed.
        pub fn new() -> Self {
            Self::default()
        }

        /// Add one source in feed order.
        pub fn add_source(&mut self, source: EventSource<'a>) -> Result<(), Error> {
            self.sources.push(source);
            Ok(())
        }

        /// Add one session; sessions stay sorted by date without duplicates.
        pub fn add_session(&mut self, session: Session) -> Result<(), Error> {
            let mut index = 0usize;
            while index < self.sessions.len() {
                if self.sessions[index].day == session.day {
                    return Err(Error::InvalidArgument);
                }
                if self.sessions[index].day > session.day {
                    break;
                }
                index += 1;
            }
            self.sessions.insert(index, session);
            Ok(())
        }

        /// The sessions in chronological order.
        pub fn sessions(&self) -> &[Session] {
            &self.sessions
        }

        /// Merge one session into `out` without collecting the other sessions.
        pub fn merge_session(
            &mut self,
            session: Session,
            out: &mut Vec<MarketEvent<'a>>,
        ) -> Result<(), Error> {
            let mut heap: Vec<HeapItem<'a>> = Vec::new();

            for order in 0..self.sources.len() {
                if let Some(day) = self.sources[order].session_day() {
                    if day != session.day {
                        continue;
                    }
                }
                let Some(mut current) = self.sources[order].peek()? else {
                    continue;
                };
                while current.timestamp_ns() < session.open_ns {
                    self.sources[order].pop()?;
                    match self.sources[order].peek()? {
                        Some(next) => current = next,
                        None => break,
                    }
                }
                if current.timestamp_ns() >= session.open_ns
                    && current.timestamp_ns() < session.close_ns
                {
                    validate_source_event(&self.sources[order], current)?;
                    heap_push(
                        &mut heap,
                        HeapItem {
                            key: SourceKey {
                                timestamp_ns: current.timestamp_ns(),
                                sequence: current.sequence(),
                                class_priority: current.class_priority(),
                            },
                            source_order: order,
                            row_order: self.sources[order].row_order(),
                            event: current,
                        },
                    );
                }
            }

            while !heap.is_empty() {
                let item = heap_pop(&mut heap);
                let source = &mut self.sources[item.source_order];
                source.pop()?;
                out.push(item.event);
                let Some(next) = source.peek()? else {
                    continue;
                };
                if next.timestamp_ns() < session.close_ns {
                    validate_source_event(source, next)?;
                    heap_push(
                        &mut heap,
                        HeapItem {
                            key: SourceKey {
                                timestamp_ns: next.timestamp_ns(),
                                sequence: next.sequence(),
                                class_priority: next.class_priority(),
                            },
                            source_order: item.source_order,
                            row_order: source.row_order(),
                            event: next,
                        },
                    );
                }
            }
            Ok(())
        }

        /// Merge every session in chronological order into `out`.
        pub fn merge_all(&mut self, out: &mut Vec<MarketEvent<'a>>) -> Result<(), Error> {
            for index in 0..self.sessions.len() {
                let session = self.sessions[index];
                self.merge_session(session, out)?;
            }
            Ok(())
        }
    }

    fn validate_source_event(
        source: &EventSource<'_>,
        event: MarketEvent<'_>,
    ) -> Result<(), Error> {
        if event.symbol() != source.symbol() {
            return Err(Error::SymbolMismatch);
        }
        if let Some(expected) = source.expected_type() {
            if event.event_type() != expected {
                return Err(Error::EventTypeMismatch);
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::replay::{EventSource, ReplayFeed, Session};
    use crate::books::{Fill, VirtualBook};
    use crate::decimal::from_int;
    use crate::fills::FillModel;
    use crate::market::{
        EventType, MarketEvent, MarketOrder, OrderType, QuoteEvent, Side, TimeInForce, TradeEvent,
    };
    use crate::market_bus::shared_clock;
    use crate::matching::SimulatedVenue;
    use crate::oms::{self, ExecutionType, OrderStatus};

    const STAMP_NS: i128 = 1_705_674_600_000_000_000;
    const MICRO: i128 = 1_000;
    const DAY_NS: i128 = 86_400 * 1_000_000_000;

    fn replay_quote(
        event_id: &'static str,
        offset_us: i128,
        sequence: u64,
    ) -> MarketEvent<'static> {
        MarketEvent::Quote(QuoteEvent {
            event_id,
            timestamp_ns: STAMP_NS + offset_us * MICRO,
            sequence,
            symbol: "SPY",
            bid_price: from_int(100),
            bid_quantity: 1_000_000_000_000_000_000,
            ask_price: from_int(101),
            ask_quantity: 1_000_000_000_000_000_000,
            venue: None,
        })
    }

    fn replay_trade(
        event_id: &'static str,
        offset_us: i128,
        sequence: u64,
    ) -> MarketEvent<'static> {
        MarketEvent::Trade(TradeEvent {
            event_id,
            timestamp_ns: STAMP_NS + offset_us * MICRO,
            sequence,
            symbol: "SPY",
            price: from_int(100) + 500_000_000_000_000_000,
            quantity: 1_000_000_000_000_000_000,
            venue: None,
            trade_id: None,
        })
    }

    #[test]
    fn same_timestamp_merge_uses_sequence_class_priority_then_feed_order() {
        let trades = [
            replay_trade("trade-sequence", 0, 2),
            replay_trade("trade-tie", 0, 4),
        ];
        let quotes = [
            replay_quote("quote-sequence", 0, 2),
            replay_quote("quote-first", 0, 4),
            replay_quote("quote-second", 0, 4),
        ];
        let mut feed = ReplayFeed::new();
        feed.add_source(EventSource::new("SPY", Some(EventType::Trade), None, &trades).unwrap())
            .unwrap();
        feed.add_source(EventSource::new("SPY", Some(EventType::Quote), None, &quotes).unwrap())
            .unwrap();
        feed.add_session(Session::new(19_741, STAMP_NS - DAY_NS, STAMP_NS + DAY_NS).unwrap())
            .unwrap();

        let mut merged = Vec::new();
        feed.merge_all(&mut merged).unwrap();

        let expected = [
            "quote-sequence",
            "trade-sequence",
            "quote-first",
            "quote-second",
            "trade-tie",
        ];
        assert_eq!(merged.len(), expected.len());
        for (id, event) in expected.iter().zip(merged.iter()) {
            assert_eq!(*id, event.event_id());
        }
    }

    #[test]
    fn source_order_must_be_monotonic_and_symbols_must_match() {
        let late = [replay_quote("late", 1, 1), replay_quote("early", 0, 2)];
        let mut feed = ReplayFeed::new();
        feed.add_source(EventSource::new("SPY", None, None, &late).unwrap())
            .unwrap();
        feed.add_session(Session::utc_day(19_741)).unwrap();
        let mut merged = Vec::new();
        assert_eq!(
            feed.merge_all(&mut merged),
            Err(super::replay::Error::RetrogradeSource)
        );

        let wrong = [MarketEvent::Quote(QuoteEvent {
            event_id: "wrong",
            timestamp_ns: STAMP_NS,
            sequence: 1,
            symbol: "QQQ",
            bid_price: from_int(100),
            bid_quantity: from_int(1),
            ask_price: from_int(101),
            ask_quantity: from_int(1),
            venue: None,
        })];
        let mut mismatch = ReplayFeed::new();
        mismatch
            .add_source(EventSource::new("SPY", None, None, &wrong).unwrap())
            .unwrap();
        mismatch.add_session(Session::utc_day(19_741)).unwrap();
        let mut out = Vec::new();
        assert_eq!(
            mismatch.merge_all(&mut out),
            Err(super::replay::Error::SymbolMismatch)
        );

        let mis_typed = [replay_quote("mis-typed", 0, 1)];
        let mut typed = ReplayFeed::new();
        typed
            .add_source(EventSource::new("SPY", Some(EventType::Trade), None, &mis_typed).unwrap())
            .unwrap();
        typed.add_session(Session::utc_day(19_741)).unwrap();
        assert_eq!(
            typed.merge_all(&mut out),
            Err(super::replay::Error::EventTypeMismatch)
        );
    }

    #[test]
    fn sessions_are_half_open_windows_and_cursors_persist() {
        let events = [
            replay_quote("before", -1, 1),
            replay_quote("open", 0, 2),
            replay_quote("inside", 30, 3),
            replay_quote("at-close", 60, 4),
            replay_quote("after", 120, 5),
        ];
        let mut feed = ReplayFeed::new();
        feed.add_source(EventSource::new("SPY", None, None, &events).unwrap())
            .unwrap();
        feed.add_session(Session::new(19_741, STAMP_NS, STAMP_NS + 60 * MICRO).unwrap())
            .unwrap();
        feed.add_session(
            Session::new(19_742, STAMP_NS + 60 * MICRO, STAMP_NS + 120 * MICRO).unwrap(),
        )
        .unwrap();

        let mut merged = Vec::new();
        feed.merge_all(&mut merged).unwrap();
        let expected = ["open", "inside", "at-close"];
        assert_eq!(merged.len(), expected.len());
        for (id, event) in expected.iter().zip(merged.iter()) {
            assert_eq!(*id, event.event_id());
        }
    }

    #[test]
    fn equal_keys_merge_in_feed_order() {
        let second = [replay_quote("second-source", 0, 1)];
        let first = [replay_quote("first-source", 0, 1)];
        let mut feed = ReplayFeed::new();
        feed.add_source(EventSource::new("SPY", Some(EventType::Quote), None, &second).unwrap())
            .unwrap();
        feed.add_source(EventSource::new("SPY", Some(EventType::Quote), None, &first).unwrap())
            .unwrap();
        feed.add_session(Session::utc_day(19_741)).unwrap();
        let mut merged = Vec::new();
        feed.merge_all(&mut merged).unwrap();
        assert_eq!(merged.len(), 2);
        assert_eq!(merged[0].event_id(), "second-source");
        assert_eq!(merged[1].event_id(), "first-source");
    }

    #[test]
    fn end_to_end_pipeline_exercises_bus_books_oms_matching_and_fills() {
        // Replay merge.
        let quote_events = [replay_quote("q1", 0, 1)];
        let trade_events = [replay_trade("t1", 0, 2)];
        let mut feed = ReplayFeed::new();
        feed.add_source(
            EventSource::new("SPY", Some(EventType::Quote), None, &quote_events).unwrap(),
        )
        .unwrap();
        feed.add_source(
            EventSource::new("SPY", Some(EventType::Trade), None, &trade_events).unwrap(),
        )
        .unwrap();
        feed.add_session(Session::utc_day(19_741)).unwrap();
        let mut merged = Vec::new();
        feed.merge_all(&mut merged).unwrap();
        assert_eq!(merged.len(), 2);

        // Matching venue turns a crossing order into reports and fills.
        let mut venue =
            SimulatedVenue::new(FillModel::SimpleTouch, 0, Some(shared_clock(STAMP_NS))).unwrap();
        venue.on_market(replay_quote("q1", 0, 1)).unwrap();
        venue.drain().unwrap();
        let order = MarketOrder {
            order_id: "e2e-buy",
            symbol: "SPY",
            side: Side::Buy,
            quantity: from_int(10),
            order_type: OrderType::Limit,
            time_in_force: TimeInForce::Day,
            limit_price: Some(from_int(101)),
            stop_price: None,
            timestamp_ns: STAMP_NS,
            sequence: 1,
        };
        venue.submit(order).unwrap();
        venue.drain().unwrap();
        assert_eq!(venue.fill_records().len(), 1);

        // OMS applies the acknowledgement and fill reports.
        let mut manager = oms::SingleLegOMS::new();
        manager
            .submit(oms::SingleLegOrder {
                order_id: "e2e-buy".to_owned(),
                symbol: "SPY".to_owned(),
                side: Side::Buy,
                quantity: from_int(10),
                order_type: OrderType::Limit,
                time_in_force: TimeInForce::Day,
            })
            .unwrap();
        let ack = oms::report_from(
            &manager.state("e2e-buy").unwrap(),
            OrderStatus::Accepted,
            ExecutionType::Acknowledged,
            "ack",
            0,
            None,
            oms::computed_average(),
        )
        .unwrap();
        manager.apply(ack).unwrap();
        let fill_report = oms::report_from(
            &manager.state("e2e-buy").unwrap(),
            OrderStatus::Filled,
            ExecutionType::Fill,
            "fill",
            from_int(10),
            Some(from_int(101)),
            oms::computed_average(),
        )
        .unwrap();
        let snapshot = manager.apply(fill_report).unwrap();
        assert_eq!(snapshot.status, OrderStatus::Filled);

        // Virtual book accounts the matching fill.
        let mut book = VirtualBook::new("e2e", from_int(10_000));
        let position = book
            .apply_fill(&Fill {
                fill_id: "f1",
                order_id: "e2e-buy",
                symbol: "SPY",
                side: Side::Buy,
                quantity: from_int(10),
                price: from_int(101),
                fee: 0,
            })
            .unwrap();
        assert_eq!(position.quantity, from_int(10));
        assert_eq!(book.fill_count(), 1);
    }
}
