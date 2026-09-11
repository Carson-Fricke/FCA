use super::*;

fn sample_snapshot() -> RuntimeSnapshot {
    RuntimeSnapshot {
        format_version: FORMAT_VERSION,
        run_id: "paper-run".to_string(),
        trigger: SnapshotTrigger::Periodic,
        sequence: 3,
        dispatched_count: 10,
        market_event_count: 24,
        options: BTreeMap::from([
            ("run_id".to_string(), JsonValue::string("paper-run")),
            ("initial_cash".to_string(), decimal_tag("1000000")),
        ]),
        books: vec![BookState {
            book_id: "alpha".to_string(),
            initial_cash: "1000000".to_string(),
            cash: "999000.00".to_string(),
            fill_ids: vec!["fill-1".to_string()],
            fills: vec![FillState {
                event_id: "e1".to_string(),
                timestamp: "2024-01-02T14:30:00.000000Z".to_string(),
                sequence: 5,
                fill_id: "fill-1".to_string(),
                order_id: "order-1".to_string(),
                symbol: "SPY".to_string(),
                side: "buy".to_string(),
                quantity: "2".to_string(),
                price: "100.00".to_string(),
                fee: "0.01".to_string(),
                liquidity: Some("taker".to_string()),
            }],
            markers: vec![("SPY".to_string(), "100.00".to_string())],
        }],
        prices: BTreeMap::from([("SPY".to_string(), "100.00".to_string())]),
        order_counts: BTreeMap::from([("momentum".to_string(), 1)]),
        order_owners: BTreeMap::from([("order-1".to_string(), "alpha".to_string())]),
        strategy_state: vec![StrategyState {
            instance_id: "momentum".to_string(),
            values: BTreeMap::from([("warm".to_string(), JsonValue::Bool(true))]),
        }],
        random_states: vec![RandomState {
            instance_id: "momentum".to_string(),
            version: 3,
            internal: vec![1, 2, 3],
            gauss_next_bits: None,
        }],
        risk: RiskState {
            on_reject: "reject_order".to_string(),
            rule_ids: vec!["max_position".to_string()],
            daily_losses: BTreeMap::from([("momentum".to_string(), "0".to_string())]),
        },
        venue: Some(VenueState {
            id_ordinal: 4,
            event_sequence: 9,
        }),
    }
}

#[test]
fn decimal_normalization_matches_python() {
    assert_eq!(decimal_text("0"), "0");
    assert_eq!(decimal_text("0.00"), "0");
    assert_eq!(decimal_text("1000000"), "1000000");
    assert_eq!(decimal_text("100.00"), "100");
    assert_eq!(decimal_text("0.0100"), "0.01");
    assert_eq!(decimal_text("-5.500"), "-5.5");
}

#[test]
fn snapshot_round_trips_byte_identically() {
    let snapshot = sample_snapshot();
    let payload = snapshot.payload_bytes();
    let reparsed =
        parse_json(std::str::from_utf8(&payload).expect("utf8"), "payload").expect("parse");
    let entries = as_object(&reparsed).expect("object");
    let restored = RuntimeSnapshot::from_json(entries.get("snapshot").expect("snapshot key"))
        .expect("restore");
    assert_eq!(restored.payload_bytes(), payload);
    assert_eq!(restored.snapshot_id(), snapshot.snapshot_id());
    assert_eq!(restored.to_json(), snapshot.to_json());
}

#[test]
fn store_save_load_round_trips() {
    let snapshot = sample_snapshot();
    let mut store = RuntimeStateStore::new("paper-run");
    store.add_store(Box::new(InMemorySnapshotStore::new()));
    let pointer = store.save(&snapshot).expect("save");
    assert_eq!(pointer.snapshot_id, snapshot.snapshot_id());
    assert_eq!(pointer.byte_length, snapshot.payload_bytes().len() as u64);
    let loaded = store.load().expect("load");
    assert_eq!(loaded.payload_bytes(), snapshot.payload_bytes());
    assert_eq!(loaded.snapshot_id(), snapshot.snapshot_id());
    let latest = store.latest().expect("latest");
    assert_eq!(latest, pointer);
}

#[test]
fn load_falls_back_to_the_mirror() {
    let snapshot = sample_snapshot();
    let mut primary = RuntimeStateStore::new("paper-run");
    primary.add_store(Box::new(InMemorySnapshotStore::new()));
    primary.save(&snapshot).expect("save");

    let mut store = RuntimeStateStore::new("paper-run");
    store.add_store(Box::new(InMemorySnapshotStore::new()));
    store.add_store(primary.stores.remove(0));
    assert_eq!(
        store.load().expect("mirror").snapshot_id(),
        snapshot.snapshot_id()
    );
}

#[test]
fn schedule_cadence() {
    let schedule = SnapshotSchedule::new(5).expect("schedule");
    assert!(!schedule.due(0));
    assert!(schedule.due(5));
    assert!(!schedule.due(6));
    assert!(schedule.due(10));
    assert!(SnapshotSchedule::new(0).is_err());
}

#[test]
fn detects_payload_tampering() {
    let snapshot = sample_snapshot();
    let mut store = RuntimeStateStore::new("paper-run");
    store.add_store(Box::new(InMemorySnapshotStore::new()));
    store.save(&snapshot).expect("save");
    let key = payload_key("paper-run", &snapshot.snapshot_id());
    store.stores[0].replace(&key, b"tampered");
    assert!(matches!(store.load(), Err(StateStoreError::Integrity(_))));
}

use crate::runtime::paper::{
    PaperOptions, PaperRuntimeShell, PaperShell, PaperShellRestore, VenueWatermarks,
};

fn paper_shell() -> PaperRuntimeShell {
    let mut shell = PaperRuntimeShell::new(
        PaperOptions::new("paper-run"),
        vec![("alpha".to_string(), "1000000".to_string())],
    )
    .with_risk("reject_order", vec!["max_position".to_string()]);
    shell.session_mut().dispatched_count = 10;
    shell.session_mut().market_event_count = 24;
    shell
        .apply_fill(
            "alpha",
            FillState {
                event_id: "e1".to_string(),
                timestamp: "2024-01-02T14:30:00.000000Z".to_string(),
                sequence: 5,
                fill_id: "fill-1".to_string(),
                order_id: "order-1".to_string(),
                symbol: "SPY".to_string(),
                side: "buy".to_string(),
                quantity: "2".to_string(),
                price: "100.00".to_string(),
                fee: "0.01".to_string(),
                liquidity: Some("taker".to_string()),
            },
        )
        .expect("fill");
    shell.count_order("momentum");
    shell.attribute_order("order-1", "alpha");
    shell.set_price("SPY", "100.00");
    shell.set_daily_loss("momentum", "0");
    shell.set_strategy_value("momentum", "warm", JsonValue::Bool(true));
    shell.set_venue(VenueWatermarks {
        id_ordinal: 4,
        event_sequence: 9,
    });
    shell
}

#[test]
fn capture_save_load_restore_round_trips_byte_identically() {
    let shell = paper_shell();
    let options = PaperOptions::new("paper-run");
    let ids = vec!["momentum".to_string()];
    let mut store = RuntimeStateStore::new("paper-run");
    store.add_store(Box::new(InMemorySnapshotStore::new()));
    let snapshot = store
        .capture(&shell, &options, &ids, SnapshotTrigger::Stop, 3, None, &[])
        .expect("capture");
    let payload = snapshot.payload_bytes();
    assert_eq!(snapshot.risk.rule_ids, vec!["max_position".to_string()]);
    assert_eq!(snapshot.order_owners["order-1"], "alpha");
    store.save(&snapshot).expect("save");
    let loaded = store.load().expect("load");
    assert_eq!(loaded.payload_bytes(), payload);
    assert_eq!(loaded.snapshot_id(), snapshot.snapshot_id());

    let restored = store
        .restore_shell(&loaded, options.clone())
        .expect("restore");
    assert_eq!(restored.books()[0].cash, shell.books()[0].cash);
    assert_eq!(restored.result().fills.len(), shell.result().fills.len());
    assert_eq!(restored.order_count("momentum"), 1);
    assert_eq!(restored.venue_watermarks().event_sequence, 9);

    let recaptured = store
        .capture(
            &restored,
            &options,
            &ids,
            SnapshotTrigger::Stop,
            3,
            None,
            &[],
        )
        .expect("recapture");
    assert_eq!(recaptured.payload_bytes(), payload);
    assert_eq!(recaptured.snapshot_id(), snapshot.snapshot_id());
}

#[test]
fn restore_rejects_a_missing_book() {
    let shell = paper_shell();
    let options = PaperOptions::new("paper-run");
    let ids = vec!["momentum".to_string()];
    let store = RuntimeStateStore::new("paper-run");
    let snapshot = store
        .capture(&shell, &options, &ids, SnapshotTrigger::Stop, 0, None, &[])
        .expect("capture");
    let mut other = PaperRuntimeShell::new(PaperOptions::new("paper-run"), vec![]);
    assert!(matches!(
        store.restore_into_shell(&snapshot, &mut other),
        Err(StateStoreError::Restore(_))
    ));
}

#[test]
fn capture_rejects_a_foreign_run() {
    let shell = paper_shell();
    let options = PaperOptions::new("paper-run");
    let store = RuntimeStateStore::new("other-run");
    assert!(matches!(
        store.capture(
            &shell,
            &options,
            &["momentum".to_string()],
            SnapshotTrigger::Stop,
            0,
            None,
            &[],
        ),
        Err(StateStoreError::Integrity(_))
    ));
}
