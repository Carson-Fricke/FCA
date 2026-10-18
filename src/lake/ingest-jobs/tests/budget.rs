//! Pure hard-limit reservation state-machine checks.
use lake_ingest_jobs::BudgetState;

#[test]
fn rolling_budget_retains_unused_grants_across_restarts_and_clock_regressions() {
    let mut state = BudgetState::new(10000).unwrap();
    let grant = state.reserve(10000, 10000, 1_000_000).unwrap();
    assert_eq!(grant.count, 10000);
    assert_eq!(grant.valid_for, std::time::Duration::from_secs(5));
    let mut restarted: BudgetState =
        serde_json::from_slice(&serde_json::to_vec(&state).unwrap()).unwrap();
    assert_eq!(
        restarted.reserve(10000, 1, 65_999_999).unwrap().count,
        0,
        "unused or lost permits cannot be refunded early"
    );
    assert_eq!(
        restarted.reserve(10000, 1, 10_000_000).unwrap().count,
        0,
        "clock rollback cannot mint permits"
    );
    assert_eq!(
        restarted.reserve(10000, 10000, 66_000_000).unwrap().count,
        10000
    );
    assert!(restarted.reserve(9999, 1, 66_000_000).is_err());
    assert!(BudgetState::new(10001).is_err());
}

#[test]
fn authority_date_precision_and_os_restarts_never_refund_early() {
    let mut state = BudgetState::new(10000).unwrap();
    state
        .reserve_with_precision(10000, 10000, 1_000_000, 1_000_000)
        .unwrap();
    assert_eq!(
        state
            .reserve_with_precision(10000, 1, 66_999_999, 1_000_000)
            .unwrap()
            .count,
        0
    );
    assert_eq!(
        state
            .reserve_with_precision(10000, 1, 67_000_000, 1_000_000)
            .unwrap()
            .count,
        1
    );

    state.hold_after_clock_restart(100, 20_000).unwrap();
    let encoded = serde_json::to_vec(&state).unwrap();
    let mut recovered: BudgetState = serde_json::from_slice(&encoded).unwrap();
    assert_eq!(
        recovered
            .reserve_with_precision(10000, 1, 65_020_099, 20_000)
            .unwrap()
            .count,
        0
    );
    assert_eq!(
        recovered
            .reserve_with_precision(10000, 10000, 65_020_100, 20_000)
            .unwrap()
            .count,
        10000
    );
}
