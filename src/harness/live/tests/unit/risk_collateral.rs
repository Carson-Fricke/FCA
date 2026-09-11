use std::collections::HashMap;

use num::Decimal18;

use super::{option_collateral_report, NoNakedShortOptions};
use crate::orders::{OrderRequest, OrderType, Side, SingleLegOrder, TimeInForce};
use crate::portfolio::hub::PortfolioHub;
use crate::risk::models::{RiskAction, RiskContext, RiskRule};
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn short_call_order() -> OrderRequest {
    OrderRequest::Single(
        SingleLegOrder::new(
            "o1",
            Symbol::new("AAPL240119C00150000").unwrap(),
            Side::Sell,
            dec("1"),
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
fn naked_short_call_is_unlimited_and_rejected() {
    let context = context();
    let report = option_collateral_report(&short_call_order(), &context, dec("100")).unwrap();
    assert!(report.unlimited());
    let rule = NoNakedShortOptions::new("no-naked", false).unwrap();
    let decision = rule.evaluate(&short_call_order(), &context).unwrap();
    assert_eq!(decision.action, RiskAction::Reject);
}

#[test]
fn covered_call_by_shares_is_not_naked() {
    let mut hub = PortfolioHub::new("acct", vec![]).unwrap();
    hub.create_book("b", dec("100000")).unwrap();
    // 100 underlying shares cover one short call.
    let shares = crate::accounting::Fill::new(
        "f1",
        "o1",
        Symbol::new("AAPL").unwrap(),
        Side::Buy,
        dec("100"),
        dec("100"),
        Decimal18::ZERO,
        None,
    )
    .unwrap();
    hub.apply_fill("b", &shares).unwrap();
    let context =
        RiskContext::new(hub.snapshot(), HashMap::new(), Decimal18::ZERO, 0, None).unwrap();
    let report = option_collateral_report(&short_call_order(), &context, dec("100")).unwrap();
    assert!(!report.naked());
    assert_eq!(report.naked_call_contracts(), Decimal18::ZERO);
}
