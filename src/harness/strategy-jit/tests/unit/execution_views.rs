//! Lazy serialization cache transitions remain independent of historical rows.
use super::*;
use harness_trading::program::ExecutionSnapshot;
use num::Decimal;
use std::sync::Arc;
fn snapshot() -> ExecutionSnapshot {
    ExecutionSnapshot {
        timestamp_ns: 1,
        initial_cash: Decimal::ONE,
        cash: Decimal::ONE,
        fees: Decimal::ZERO,
        equity: Some(Decimal::ONE),
        realized_pnl: Decimal::ZERO,
        unrealized_pnl: Some(Decimal::ZERO),
        gross_exposure: Some(Decimal::ZERO),
        positions: vec![],
        orders: Arc::new(vec![]),
        closed_trades: Arc::new(vec![]),
    }
}
#[test]
fn updates_never_serialize_unused_views_and_only_invalidate_changed_histories() {
    let mut views = ExecutionViews::default();
    let mut state = snapshot();
    views.update(&state);
    assert!(
        views.account.get().is_none()
            && views.orders.get().is_none()
            && views.trades.get().is_none()
    );
    let account = unsafe { read_execution(views.source().ctx, 0).as_str() }.unwrap();
    assert!(account.contains("timestamp_ns"));
    assert!(views.orders.get().is_none() && views.trades.get().is_none());
    unsafe {
        read_execution(views.source().ctx, 1);
    }
    assert!(views.orders.get().is_some());
    state.timestamp_ns = 2;
    views.update(&state);
    assert!(views.account.get().is_none());
    assert!(
        views.orders.get().is_some(),
        "unchanged immutable history retains its lazy cache"
    );
    assert!(views.trades.get().is_none());
    state.orders = Arc::new(vec![]);
    views.update(&state);
    assert!(
        views.orders.get().is_none(),
        "changed histories invalidate without re-encoding"
    );
}
