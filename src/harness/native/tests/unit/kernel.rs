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

fn replay_quote(event_id: &'static str, offset_us: i128, sequence: u64) -> MarketEvent<'static> {
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

fn replay_trade(event_id: &'static str, offset_us: i128, sequence: u64) -> MarketEvent<'static> {
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
    feed.add_session(Session::new(19_742, STAMP_NS + 60 * MICRO, STAMP_NS + 120 * MICRO).unwrap())
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
    feed.add_source(EventSource::new("SPY", Some(EventType::Quote), None, &quote_events).unwrap())
        .unwrap();
    feed.add_source(EventSource::new("SPY", Some(EventType::Trade), None, &trade_events).unwrap())
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
