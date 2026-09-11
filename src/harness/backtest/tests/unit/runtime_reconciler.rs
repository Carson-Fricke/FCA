use super::*;

#[test]
fn exact_match_reports_no_deltas() {
    let expected = vec![ExpectedPosition {
        symbol: "SPY".to_string(),
        quantity: "10".to_string(),
    }];
    let actual = vec![BrokerPosition {
        symbol: "SPY".to_string(),
        quantity: "10".to_string(),
    }];
    let deltas = reconcile_positions(&expected, &actual, ReconciliationTolerances::default());
    assert!(deltas.is_empty());
}

#[test]
fn reports_signed_differences() {
    let expected = vec![ExpectedPosition {
        symbol: "SPY".to_string(),
        quantity: "10".to_string(),
    }];
    let actual = vec![BrokerPosition {
        symbol: "SPY".to_string(),
        quantity: "8".to_string(),
    }];
    let deltas = reconcile_positions(&expected, &actual, ReconciliationTolerances::default());
    assert_eq!(deltas.len(), 1);
    assert_eq!(deltas[0].difference, "2.000000000000000000");
}

#[test]
fn tolerance_suppresses_small_differences() {
    let expected = vec![ExpectedPosition {
        symbol: "SPY".to_string(),
        quantity: "10.00".to_string(),
    }];
    let actual = vec![BrokerPosition {
        symbol: "SPY".to_string(),
        quantity: "10.000000000000000001".to_string(),
    }];
    let tolerances = ReconciliationTolerances {
        position_abs: Decimal18::from_unscaled(10).expect("tiny"),
        cash_abs: Decimal18::ZERO,
    };
    assert!(reconcile_positions(&expected, &actual, tolerances).is_empty());
}

#[test]
fn schedule_cadence() {
    let schedule = ReconcileSchedule::new(4).expect("schedule");
    assert!(!schedule.due(0));
    assert!(schedule.due(4));
    assert!(!schedule.due(5));
}
