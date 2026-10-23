//! Malformed, overflowed, and unknown values on the paper, shadow, reconciler,
//! and state-store surfaces fail by name instead of reading as zero.
//!
//! Each test drives one public runtime entry point: `PaperBook::apply_fill`,
//! `PaperRuntimeShell::apply_fill`, `build_report`, `reconcile_positions`,
//! `reconcile_cash`, and the state store's capture and restore. Valid zeros keep
//! their meaning and are asserted next to the failures.

use std::collections::BTreeMap;

use harness_contracts::canonical_json::JsonValue;
use harness_trading::replay_errors::BacktestError;
use live::runtime::paper::PaperBook;
use live::runtime::paper::PaperOptions;
use live::runtime::paper::PaperRuntimeShell;
use live::runtime::paper::PaperSessionResult;
use live::runtime::paper::PaperShell;
use live::runtime::paper::PaperShellRestore;
use live::runtime::reconciler::reconcile_cash;
use live::runtime::reconciler::reconcile_positions;
use live::runtime::reconciler::BrokerPosition;
use live::runtime::reconciler::ExpectedPosition;
use live::runtime::reconciler::ReconciliationTolerances;
use live::runtime::shadow::build_report;
use live::runtime::shadow::LivePrint;
use live::runtime::shadow::ObserveOnlyBrokerSink;
use live::runtime::shadow::SingleLegOrder;
use live::runtime::state_store::decimal_text;
use live::runtime::state_store::FillState;
use live::runtime::state_store::RuntimeStateStore;
use live::runtime::state_store::SnapshotTrigger;
use live::runtime::state_store::StateStoreError;
use num::Decimal18;

/// A fill for one order, in the shape a session records it.
fn booked(
    order_id: &str,
    fill_id: &str,
    side: &str,
    quantity: &str,
    price: &str,
    fee: &str,
) -> FillState {
    FillState {
        event_id: format!("event-{fill_id}"),
        timestamp: "2024-01-02T14:30:00.000000Z".to_string(),
        sequence: 1,
        fill_id: fill_id.to_string(),
        order_id: order_id.to_string(),
        symbol: "SPY".to_string(),
        side: side.to_string(),
        quantity: quantity.to_string(),
        price: price.to_string(),
        fee: fee.to_string(),
        liquidity: None,
    }
}

fn live(order_id: &str, side: &str, quantity: &str, price: &str) -> LivePrint {
    LivePrint {
        order_id: order_id.to_string(),
        symbol: "SPY".to_string(),
        side: side.to_string(),
        quantity: quantity.to_string(),
        price: price.to_string(),
        timestamp_ns: 1,
    }
}

fn session(fills: Vec<FillState>) -> PaperSessionResult {
    PaperSessionResult {
        fills,
        ..PaperSessionResult::default()
    }
}

fn book_cash(book: &PaperBook) -> Decimal18 {
    book.cash.parse().expect("booked cash is an exact decimal")
}

#[test]
fn malformed_fill_quantity_fails_by_name_and_leaves_the_book_unchanged() {
    let mut book = PaperBook::new("book-a", "1000");
    let error = book
        .apply_fill(&booked("order-1", "fill-1", "buy", "ten", "10", "0"))
        .expect_err("a malformed quantity must not be booked");
    assert!(matches!(error, BacktestError::DataPreflight(_)));
    assert_eq!(book.cash, "1000");
    assert!(book.fills.is_empty());
    assert!(book.markers.is_empty());
}

#[test]
fn notional_outside_the_decimal_bound_fails_by_name_and_leaves_the_book_unchanged() {
    let mut book = PaperBook::new("book-a", "1000");
    let error = book
        .apply_fill(&booked(
            "order-1",
            "fill-1",
            "buy",
            "1000000000000000",
            "1000000000",
            "0",
        ))
        .expect_err("a notional above 1e20 cannot be booked exactly");
    assert!(matches!(error, BacktestError::Arithmetic(_)));
    assert_eq!(book.cash, "1000");
    assert!(book.markers.is_empty());
}

#[test]
fn unknown_side_fails_by_name_instead_of_booking_as_a_buy() {
    let mut book = PaperBook::new("book-a", "1000");
    let error = book
        .apply_fill(&booked("order-1", "fill-1", "short", "1", "10", "0"))
        .expect_err("a side that is neither buy nor sell has no cash direction");
    assert!(matches!(error, BacktestError::DataPreflight(_)));
    assert_eq!(book.cash, "1000");
}

#[test]
fn duplicate_fill_is_rejected_once_booked() {
    let mut book = PaperBook::new("book-a", "1000");
    book.apply_fill(&booked("order-1", "fill-1", "buy", "1", "10", "0"))
        .expect("first booking");
    let error = book
        .apply_fill(&booked("order-1", "fill-1", "buy", "1", "10", "0"))
        .expect_err("a duplicate booking");
    assert!(matches!(error, BacktestError::DataPreflight(_)));
    assert_eq!(book.fills.len(), 1);
}

#[test]
fn cash_reduced_to_exact_zero_reads_back_as_zero() {
    // Zero is a valid cash balance: the zero a fill leaves behind must parse
    // back as zero on the next fill.
    let mut book = PaperBook::new("book-a", "10");
    book.apply_fill(&booked("order-1", "fill-1", "buy", "1", "10", "0"))
        .expect("buy to zero");
    assert_eq!(book_cash(&book), Decimal18::ZERO);
    book.apply_fill(&booked("order-2", "fill-2", "sell", "2", "1", "0"))
        .expect("sell from zero");
    assert_eq!(book_cash(&book), Decimal18::from_i64(2));
}

#[test]
fn fill_to_an_unknown_book_is_a_configuration_error() {
    let mut shell = PaperRuntimeShell::new(
        PaperOptions::new("job-1"),
        vec![("book-a".to_string(), "1000".to_string())],
    );
    let error = shell
        .apply_fill(
            "book-missing",
            booked("order-1", "fill-1", "buy", "1", "10", "0"),
        )
        .expect_err("the book is not configured");
    assert!(matches!(error, BacktestError::Configuration(_)));
    assert_eq!(shell.result().fill_count, 0);
}

#[test]
fn matching_live_print_is_a_clean_shadow_match() {
    let mut sink = ObserveOnlyBrokerSink::new();
    let intent = sink.submit(
        &SingleLegOrder::new("order-1", "SPY", "buy", "1"),
        Some("strategy-a"),
    );
    let report = build_report(
        "job-1",
        "shadow",
        session(vec![booked("order-1", "fill-1", "buy", "1", "100", "0")]),
        &[intent],
        &[live("order-1", "buy", "1", "100")],
    )
    .expect("exact prints compare");
    assert!(report.parity_clean());
    assert_eq!(report.matched_count(), 1);
}

#[test]
fn malformed_live_quantity_fails_the_shadow_report_instead_of_diverging() {
    let mut sink = ObserveOnlyBrokerSink::new();
    let intent = sink.submit(&SingleLegOrder::new("order-1", "SPY", "buy", "1"), None);
    let error = build_report(
        "job-1",
        "shadow",
        session(vec![booked("order-1", "fill-1", "buy", "1", "100", "0")]),
        &[intent],
        &[live("order-1", "buy", "one", "100")],
    )
    .expect_err("a malformed live quantity has no comparison");
    assert!(matches!(error, BacktestError::DataPreflight(_)));
}

#[test]
fn would_be_quantity_sum_outside_the_decimal_bound_fails_by_name() {
    let mut sink = ObserveOnlyBrokerSink::new();
    let intent = sink.submit(&SingleLegOrder::new("order-1", "SPY", "buy", "1"), None);
    let fills = vec![
        booked(
            "order-1",
            "fill-1",
            "buy",
            "60000000000000000000",
            "100",
            "0",
        ),
        booked(
            "order-1",
            "fill-2",
            "buy",
            "60000000000000000000",
            "100",
            "0",
        ),
    ];
    let error = build_report(
        "job-1",
        "shadow",
        session(fills),
        &[intent],
        &[live("order-1", "buy", "1", "100")],
    )
    .expect_err("the summed quantity leaves the decimal bound");
    assert!(matches!(error, BacktestError::Arithmetic(_)));
}

#[test]
fn malformed_broker_quantity_fails_reconciliation_by_name() {
    let expected = [ExpectedPosition {
        symbol: "SPY".to_string(),
        quantity: "10".to_string(),
    }];
    let actual = [BrokerPosition {
        symbol: "SPY".to_string(),
        quantity: "ten".to_string(),
    }];
    let error = reconcile_positions(&expected, &actual, ReconciliationTolerances::default())
        .expect_err("a malformed broker quantity");
    assert!(matches!(error, BacktestError::DataPreflight(_)));
}

#[test]
fn position_absent_from_the_broker_is_a_flat_valid_zero() {
    let expected = [ExpectedPosition {
        symbol: "SPY".to_string(),
        quantity: "10".to_string(),
    }];
    let deltas = reconcile_positions(&expected, &[], ReconciliationTolerances::default())
        .expect("an absent position is flat");
    assert_eq!(deltas.len(), 1);
    assert_eq!(deltas[0].actual, "0");
}

#[test]
fn malformed_broker_cash_fails_reconciliation_by_name() {
    let error = reconcile_cash("1000", "", ReconciliationTolerances::default())
        .expect_err("empty cash is not an exact decimal");
    assert!(matches!(error, BacktestError::DataPreflight(_)));
}

#[test]
fn cash_difference_outside_the_decimal_bound_fails_instead_of_matching() {
    let error = reconcile_cash(
        "90000000000000000000",
        "-90000000000000000000",
        ReconciliationTolerances::default(),
    )
    .expect_err("the difference leaves the decimal bound");
    assert!(matches!(error, BacktestError::Arithmetic(_)));
}

#[test]
fn restore_accepts_the_captured_snapshot_and_rejects_a_malformed_cash() {
    let options = PaperOptions::new("job-1");
    let books = vec![("book-a".to_string(), "1000".to_string())];
    let shell = PaperRuntimeShell::new(options.clone(), books.clone());
    let store = RuntimeStateStore::new("job-1");
    let mut snapshot = store
        .capture(
            &shell,
            &options,
            &["strategy-a".to_string()],
            SnapshotTrigger::Periodic,
            1,
            None,
            &[],
        )
        .expect("capture");
    store
        .restore_into_shell(
            &snapshot,
            &mut PaperRuntimeShell::new(options.clone(), books.clone()),
        )
        .expect("the captured snapshot restores");
    snapshot.books[0].cash = "not-a-number".to_string();
    let error = store
        .restore_into_shell(&snapshot, &mut PaperRuntimeShell::new(options, books))
        .expect_err("a malformed captured cash must not restore");
    assert!(matches!(error, StateStoreError::Integrity(_)));
}

#[test]
fn capture_rejects_strategy_state_that_is_not_an_object() {
    let options = PaperOptions::new("job-1");
    let mut shell = PaperRuntimeShell::new(
        options.clone(),
        vec![("book-a".to_string(), "1000".to_string())],
    );
    shell.restore_strategy_state(BTreeMap::from([(
        "strategy-a".to_string(),
        JsonValue::Int(7),
    )]));
    let error = RuntimeStateStore::new("job-1")
        .capture(
            &shell,
            &options,
            &["strategy-a".to_string()],
            SnapshotTrigger::Periodic,
            1,
            None,
            &[],
        )
        .expect_err("exported strategy state must be an object");
    assert!(matches!(error, StateStoreError::Integrity(_)));
}

#[test]
fn empty_decimal_text_is_not_canonicalized_to_zero() {
    assert_eq!(decimal_text(""), "");
    assert_eq!(decimal_text("0.000"), "0");
}
