use super::*;

fn fill(fill_id: &str, order_id: &str, side: &str, quantity: &str, price: &str) -> FillState {
    FillState {
        event_id: "e".to_string(),
        timestamp: "2024-01-02T14:30:00.000000Z".to_string(),
        sequence: 0,
        fill_id: fill_id.to_string(),
        order_id: order_id.to_string(),
        symbol: "SPY".to_string(),
        side: side.to_string(),
        quantity: quantity.to_string(),
        price: price.to_string(),
        fee: "0".to_string(),
        liquidity: None,
    }
}

#[test]
fn sink_records_intents_without_transmitting() {
    let mut sink = ObserveOnlyBrokerSink::new();
    let order = SingleLegOrder::new("o1", "SPY", "buy", "10");
    let intent = sink.submit(&order, Some("momentum"));
    assert_eq!(intent.sequence, 1);
    assert_eq!(sink.len(), 1);
    assert_eq!(sink.intents()[0].strategy_id.as_deref(), Some("momentum"));
}

#[test]
fn matches_identical_live_prints() {
    let mut sink = ObserveOnlyBrokerSink::new();
    sink.submit(&SingleLegOrder::new("o1", "SPY", "buy", "10"), None);
    let mut session = PaperSessionResult::default();
    session.fills.push(fill("f1", "o1", "buy", "10", "100"));
    let prints = vec![LivePrint {
        order_id: "o1".to_string(),
        symbol: "SPY".to_string(),
        side: "buy".to_string(),
        quantity: "10".to_string(),
        price: "100".to_string(),
        timestamp_ns: 5,
    }];
    let report = build_report("run", "shadow", session, sink.intents(), &prints);
    assert!(report.parity_clean());
    assert_eq!(report.matched_count(), 1);
    assert_eq!(report.comparisons[0].verdict, ShadowVerdict::Matched);
}

#[test]
fn reports_divergence_and_live_only_prints() {
    let mut sink = ObserveOnlyBrokerSink::new();
    sink.submit(&SingleLegOrder::new("o1", "SPY", "buy", "10"), None);
    let mut session = PaperSessionResult::default();
    session.fills.push(fill("f1", "o1", "buy", "10", "100"));
    let prints = vec![
        LivePrint {
            order_id: "o1".to_string(),
            symbol: "SPY".to_string(),
            side: "buy".to_string(),
            quantity: "10".to_string(),
            price: "101".to_string(),
            timestamp_ns: 5,
        },
        LivePrint {
            order_id: "o2".to_string(),
            symbol: "QQQ".to_string(),
            side: "sell".to_string(),
            quantity: "1".to_string(),
            price: "5".to_string(),
            timestamp_ns: 6,
        },
    ];
    let report = build_report("run", "shadow", session, sink.intents(), &prints);
    assert!(!report.parity_clean());
    assert_eq!(report.diverged_count(), 1);
    assert_eq!(report.live_only_count(), 1);
    assert!(report.summary().contains("diverged"));
}

#[test]
fn unfilled_and_would_be_only_are_informational() {
    let mut sink = ObserveOnlyBrokerSink::new();
    sink.submit(&SingleLegOrder::new("o1", "SPY", "buy", "10"), None);
    sink.submit(&SingleLegOrder::new("o2", "SPY", "buy", "5"), None);
    let mut session = PaperSessionResult::default();
    session.fills.push(fill("f1", "o2", "buy", "5", "100"));
    let report = build_report("run", "shadow", session, sink.intents(), &[]);
    assert!(report.parity_clean());
    assert_eq!(report.unfilled_count(), 1);
    assert_eq!(report.would_be_only_count(), 1);
}
