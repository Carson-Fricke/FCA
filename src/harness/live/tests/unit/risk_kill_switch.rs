use std::collections::HashMap;

use num::Decimal18;

use super::{KillSwitch, KillSwitchAction, KillSwitchState};
use crate::orders::{OrderRequest, OrderType, Side, SingleLegOrder, TimeInForce};
use crate::portfolio::hub::PortfolioHub;
use crate::risk::models::{RiskAction, RiskContext, RiskRule};
use crate::symbol::Symbol;

fn order() -> OrderRequest {
    OrderRequest::Single(
        SingleLegOrder::new(
            "o1",
            Symbol::new("AAPL").unwrap(),
            Side::Buy,
            "1".parse().unwrap(),
            OrderType::Market,
            TimeInForce::Day,
            None,
            None,
            None,
        )
        .unwrap(),
    )
}

fn context() -> RiskContext {
    let hub = PortfolioHub::new("acct", vec![]).unwrap();
    RiskContext::new(hub.snapshot(), HashMap::new(), Decimal18::ZERO, 0, None).unwrap()
}

#[test]
fn disengaged_switch_allows_and_trip_rejects_with_halt() {
    let mut switch = KillSwitch::new(None).unwrap();
    let allowed = switch.evaluate(&order(), &context()).unwrap();
    assert_eq!(allowed.action, RiskAction::Allow);
    switch.trip(Some("manual"), Some("operator")).unwrap();
    assert_eq!(switch.state(), KillSwitchState::Engaged);
    let rejected = switch.evaluate(&order(), &context()).unwrap();
    assert_eq!(rejected.action, RiskAction::Reject);
    assert!(rejected.halt);
    switch.release(None, None).unwrap();
    assert_eq!(switch.state(), KillSwitchState::Disengaged);
    assert_eq!(switch.events().len(), 2);
    assert_eq!(switch.events()[0].action, KillSwitchAction::Trip);
}
