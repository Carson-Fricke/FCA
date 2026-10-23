//! Exact-decimal risk allowances must remain within the cap after rounding.
use harness_trading::{
    orders::{OrderRequest, OrderType, Side, SingleLegOrder, TimeInForce},
    portfolio::hub::PortfolioHub,
    risk::{
        limits::MaxOrderNotional,
        models::{RiskAction, RiskContext, RiskRule},
    },
    symbol::Symbol,
};
use num::Decimal18;
#[test]
fn live_risk_reduces_within_the_cap_and_a_recheck_allows_the_result() {
    let price: Decimal18 = "450.015".parse().unwrap();
    let symbol = Symbol::new("SPY").unwrap();
    let context = RiskContext::new(
        PortfolioHub::new("account", vec![]).unwrap().snapshot(),
        [(symbol.clone(), price)].into(),
        Decimal18::ZERO,
        0,
        None,
    )
    .unwrap();
    let order = OrderRequest::Single(
        SingleLegOrder::new(
            "order",
            symbol,
            Side::Buy,
            Decimal18::from_i64(10),
            OrderType::Market,
            TimeInForce::Gtc,
            None,
            None,
            None,
        )
        .unwrap(),
    );
    let rule = MaxOrderNotional::new("cap", Decimal18::from_i64(500)).unwrap();
    let result = rule.evaluate(&order, &context).unwrap();
    assert_eq!(result.action, RiskAction::Reduce);
    let OrderRequest::Single(ref reduced) = result.order else {
        panic!("single order")
    };
    assert!(reduced.quantity * price <= Decimal18::from_i64(500));
    assert_eq!(
        rule.evaluate(&result.order, &context).unwrap().action,
        RiskAction::Allow
    );
}
