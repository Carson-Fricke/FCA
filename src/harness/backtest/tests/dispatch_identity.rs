//! Canonical dispatch hashing must survive allocation and timestamp caching changes.
use backtest::backtest::event_bus::{EngineEvent, EventBus};
use harness_trading::program::EngineMarket;
use num::Decimal;

#[test]
fn dispatch_digest_retains_negative_nanosecond_boundaries_and_full_sequence_range() {
    let mut bus = EventBus::new();
    let timestamps = [
        -1, 0, 999, 1000, 999_999_999, 1_000_000_000, 1_000_000_999,
        i64::MIN, i64::MAX,
    ];
    for (index, timestamp_ns) in timestamps.into_iter().enumerate() {
        bus.schedule(EngineEvent::Market(EngineMarket::Trade {
            event_id: format!("feed/π/{index}"),
            timestamp_ns,
            sequence: if index == 8 { u64::MAX } else { index as u64 },
            symbol: "MSFT".into(),
            price: Decimal::ONE,
            quantity: Decimal::ONE,
            venue: "NASDAQ".into(),
            trade_id: index.to_string(),
        }));
        assert!(bus.dispatch_next().is_some());
    }
    assert_eq!(bus.dispatched_count(), 9);
    assert_eq!(
        bus.dispatch_hash(),
        "e6d22a7469854e537d57fa1a214d0af3370bcc16aec466a43490f6cadf4acd80"
    );
}

#[test]
fn feed_head_and_generated_backlog_preserve_all_ordering_ties() {
    use harness_trading::program::EngineReport;
    let mut bus = EventBus::new();
    let mut events = Vec::new();
    for (index, (time, sequence)) in [(10, 4), (10, 4), (1, 9), (10, 3), (11, 0)].into_iter().enumerate() {
        events.push(EngineEvent::Market(EngineMarket::Trade {
            event_id: format!("market-{index}"), timestamp_ns: time, sequence,
            symbol: "MSFT".into(), price: Decimal::ONE, quantity: Decimal::ONE,
            venue: "NASDAQ".into(), trade_id: index.to_string(),
        }));
        events.push(EngineEvent::Report(EngineReport {
            event_id: format!("report-{index}"), timestamp_ns: time, sequence,
            order_id: "order".into(), execution_type: "acknowledged".into(),
        }));
    }
    for event in &events { bus.schedule(event.clone()); }
    events.sort_by_key(|event| (event.timestamp_ns(), event.sequence(), event.class_priority()));
    let mut dispatched = Vec::new();
    while let Some(event) = bus.dispatch_next() { dispatched.push(event); }
    assert_eq!(dispatched, events);
}
