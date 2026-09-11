use super::*;

fn fill(fill_id: &str, symbol: &str, side: &str, quantity: &str, price: &str) -> FillState {
    FillState {
        event_id: "e".to_string(),
        timestamp: "2024-01-02T14:30:00.000000Z".to_string(),
        sequence: 0,
        fill_id: fill_id.to_string(),
        order_id: format!("order-{fill_id}"),
        symbol: symbol.to_string(),
        side: side.to_string(),
        quantity: quantity.to_string(),
        price: price.to_string(),
        fee: "0".to_string(),
        liquidity: None,
    }
}

#[test]
fn session_policy_round_trips() {
    for policy in [PaperSessionPolicy::Continuous, PaperSessionPolicy::Session] {
        assert_eq!(PaperSessionPolicy::parse(policy.value()), Some(policy));
    }
}

#[test]
fn options_defaults_match_python() {
    let options = PaperOptions::new("run");
    assert_eq!(options.initial_cash, "1000000");
    assert_eq!(options.abort_policy, AbortPolicy::Raise);
    assert_eq!(options.session_policy, PaperSessionPolicy::Session);
}

#[test]
fn book_applies_fills_and_tracks_marks() {
    let mut book = PaperBook::new("alpha", "100000");
    book.apply_fill(&fill("f1", "SPY", "buy", "2", "100"))
        .expect("fill");
    assert_eq!(book.fills.len(), 1);
    assert_eq!(book.fill_ids(), vec!["f1".to_string()]);
    assert_eq!(book.markers, vec![("SPY".to_string(), "100".to_string())]);
    assert_eq!(book.cash, "99800.000000000000000000");
    assert!(book
        .apply_fill(&fill("f1", "SPY", "buy", "2", "100"))
        .is_err());
}

#[test]
fn shell_supervision_surface_reads_back() {
    let mut shell = PaperRuntimeShell::new(
        PaperOptions::new("run"),
        vec![("alpha".to_string(), "100000".to_string())],
    )
    .with_risk("reject_order", vec!["max_position".to_string()]);
    shell.session_mut().dispatched_count = 3;
    shell.session_mut().market_event_count = 9;
    shell
        .apply_fill("alpha", fill("f1", "SPY", "buy", "2", "100"))
        .expect("fill");
    shell.count_order("momentum");
    shell.attribute_order("order-f1", "momentum");
    shell.set_price("SPY", "100");
    shell.set_daily_loss("momentum", "0");
    shell.set_venue(VenueWatermarks {
        id_ordinal: 4,
        event_sequence: 9,
    });
    assert_eq!(shell.result().fill_count, 1);
    assert_eq!(shell.order_count("momentum"), 1);
    assert_eq!(shell.venue_watermarks().event_sequence, 9);
    assert_eq!(shell.on_reject(), "reject_order");
}
