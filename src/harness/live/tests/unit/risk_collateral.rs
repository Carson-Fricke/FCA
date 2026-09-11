use std::collections::HashMap;

use num::Decimal18;

use super::{option_collateral_report, NoNakedShortOptions, OptionCollateralLimit};
use crate::accounting::Fill;
use crate::orders::{
    MultiLegOrder, OrderLeg, OrderRequest, OrderType, Side, SingleLegOrder, TimeInForce,
};
use crate::portfolio::hub::PortfolioHub;
use crate::risk::models::{RiskAction, RiskContext, RiskRule};
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn osi(root: &str, expiry: &str, right: char, strike: u32) -> String {
    format!("{root}{expiry}{right}{:08}", strike * 1000)
}

fn single(symbol: &str, side: Side, quantity: &str) -> OrderRequest {
    OrderRequest::Single(
        SingleLegOrder::new(
            "order-1",
            Symbol::new(symbol).unwrap(),
            side,
            dec(quantity),
            OrderType::Market,
            TimeInForce::Day,
            None,
            None,
            None,
        )
        .unwrap(),
    )
}

fn multi(legs: &[(&str, Side, &str)]) -> OrderRequest {
    let legs = legs
        .iter()
        .map(|(symbol, side, quantity)| {
            OrderLeg::new(
                Symbol::new(symbol).unwrap(),
                *side,
                dec(quantity),
                dec("1"),
                None,
            )
            .unwrap()
        })
        .collect();
    OrderRequest::Multi(
        MultiLegOrder::new(
            "order-1",
            legs,
            OrderType::Market,
            TimeInForce::Day,
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

fn context_with(positions: &[(&str, &str, &str)]) -> RiskContext {
    let mut hub = PortfolioHub::new("acct", vec![]).unwrap();
    hub.create_book("b", dec("1000000")).unwrap();
    for (index, (symbol, quantity, price)) in positions.iter().enumerate() {
        let signed = dec(quantity);
        let side = if signed.is_negative() {
            Side::Sell
        } else {
            Side::Buy
        };
        let size = if signed.is_negative() {
            -signed
        } else {
            signed
        };
        let fill = Fill::new(
            &format!("f{index}"),
            &format!("o{index}"),
            Symbol::new(symbol).unwrap(),
            side,
            size,
            dec(price),
            Decimal18::ZERO,
            None,
        )
        .unwrap();
        hub.apply_fill("b", &fill).unwrap();
    }
    RiskContext::new(hub.snapshot(), HashMap::new(), Decimal18::ZERO, 0, None).unwrap()
}

#[test]
fn vertical_call_spread_collateral_is_exactly_the_width() {
    let order = multi(&[
        (&osi("SPY", "241119", 'C', 500), Side::Sell, "1"),
        (&osi("SPY", "241119", 'C', 505), Side::Buy, "1"),
    ]);
    let report = option_collateral_report(&order, &context(), dec("100")).unwrap();
    assert_eq!(report.total_collateral(), Some(dec("500")));
    assert!(!report.unlimited());
    assert!(!report.naked());
    let record = &report.groups[0].contracts[0];
    assert_eq!(record.symbol, osi("SPY", "241119", 'C', 500));
    assert!(record.covered_by_long_option);
    assert_eq!(record.naked_contracts, Decimal18::ZERO);

    let allowed = OptionCollateralLimit::new("limit", dec("500"))
        .unwrap()
        .evaluate(&order, &context())
        .unwrap();
    assert_eq!(allowed.action, RiskAction::Allow);
    let rejected = OptionCollateralLimit::new("limit", dec("499.99"))
        .unwrap()
        .evaluate(&order, &context())
        .unwrap();
    assert_eq!(rejected.action, RiskAction::Reject);
}

#[test]
fn bull_put_spread_reserves_the_width_without_nakedness() {
    let order = multi(&[
        (&osi("SPY", "241119", 'P', 450), Side::Sell, "1"),
        (&osi("SPY", "241119", 'P', 440), Side::Buy, "1"),
    ]);
    let report = option_collateral_report(&order, &context(), dec("100")).unwrap();
    assert_eq!(report.total_collateral(), Some(dec("1000")));
    assert_eq!(report.naked_put_contracts(), Decimal18::ZERO);
    let decision = NoNakedShortOptions::new("no-naked", false)
        .unwrap()
        .evaluate(&order, &context())
        .unwrap();
    assert_eq!(decision.action, RiskAction::Allow);
}

#[test]
fn bear_put_spread_requires_no_additional_collateral() {
    let order = multi(&[
        (&osi("SPY", "241119", 'P', 450), Side::Sell, "1"),
        (&osi("SPY", "241119", 'P', 460), Side::Buy, "1"),
    ]);
    let report = option_collateral_report(&order, &context(), dec("100")).unwrap();
    assert_eq!(report.total_collateral(), Some(Decimal18::ZERO));
}

#[test]
fn iron_condor_reserves_exactly_the_wider_width() {
    let order = multi(&[
        (&osi("SPY", "241119", 'P', 440), Side::Buy, "1"),
        (&osi("SPY", "241119", 'P', 450), Side::Sell, "1"),
        (&osi("SPY", "241119", 'C', 510), Side::Sell, "1"),
        (&osi("SPY", "241119", 'C', 520), Side::Buy, "1"),
    ]);
    let report = option_collateral_report(&order, &context(), dec("100")).unwrap();
    assert_eq!(report.total_collateral(), Some(dec("1000")));
    assert!(!report.naked());

    let asymmetric = multi(&[
        (&osi("SPY", "241119", 'P', 430), Side::Buy, "1"),
        (&osi("SPY", "241119", 'P', 450), Side::Sell, "1"),
        (&osi("SPY", "241119", 'C', 510), Side::Sell, "1"),
        (&osi("SPY", "241119", 'C', 515), Side::Buy, "1"),
    ]);
    let report = option_collateral_report(&asymmetric, &context(), dec("100")).unwrap();
    assert_eq!(report.total_collateral(), Some(dec("2000")));
}

#[test]
fn naked_short_call_is_unlimited_and_rejected() {
    let order = single(&osi("SPY", "241119", 'C', 500), Side::Sell, "1");
    let report = option_collateral_report(&order, &context(), dec("100")).unwrap();
    assert!(report.unlimited());
    assert_eq!(report.total_collateral(), None);
    assert_eq!(report.naked_call_contracts(), dec("1"));
    let decision = NoNakedShortOptions::new("no-naked", false)
        .unwrap()
        .evaluate(&order, &context())
        .unwrap();
    assert_eq!(decision.action, RiskAction::Reject);
    let capped = OptionCollateralLimit::new("limit", dec("1000000"))
        .unwrap()
        .evaluate(&order, &context())
        .unwrap();
    assert_eq!(capped.action, RiskAction::Reject);
}

#[test]
fn naked_short_put_is_rejected_by_default_and_cash_secured_when_allowed() {
    let order = single(&osi("SPY", "241119", 'P', 450), Side::Sell, "1");
    let report = option_collateral_report(&order, &context(), dec("100")).unwrap();
    assert_eq!(report.total_collateral(), Some(dec("45000")));
    assert_eq!(report.naked_put_contracts(), dec("1"));
    let decision = NoNakedShortOptions::new("no-naked", false)
        .unwrap()
        .evaluate(&order, &context())
        .unwrap();
    assert_eq!(decision.action, RiskAction::Reject);

    let permissive = NoNakedShortOptions::new("no-naked", true)
        .unwrap()
        .evaluate(&order, &context())
        .unwrap();
    assert_eq!(permissive.action, RiskAction::Allow);
    let allowed = OptionCollateralLimit::new("limit", dec("45000"))
        .unwrap()
        .evaluate(&order, &context())
        .unwrap();
    assert_eq!(allowed.action, RiskAction::Allow);
    let rejected = OptionCollateralLimit::new("limit", dec("44999.99"))
        .unwrap()
        .evaluate(&order, &context())
        .unwrap();
    assert_eq!(rejected.action, RiskAction::Reject);
}

#[test]
fn share_covered_call_is_not_naked() {
    let state = context_with(&[("SPY", "100", "450")]);
    let order = single(&osi("SPY", "241119", 'C', 500), Side::Sell, "1");
    let report = option_collateral_report(&order, &state, dec("100")).unwrap();
    assert_eq!(report.total_collateral(), Some(Decimal18::ZERO));
    assert!(!report.naked());
    let decision = NoNakedShortOptions::new("no-naked", false)
        .unwrap()
        .evaluate(&order, &state)
        .unwrap();
    assert_eq!(decision.action, RiskAction::Allow);
}

#[test]
fn partial_share_cover_leaves_unlimited_risk() {
    let state = context_with(&[("SPY", "100", "450")]);
    let order = single(&osi("SPY", "241119", 'C', 500), Side::Sell, "2");
    let report = option_collateral_report(&order, &state, dec("100")).unwrap();
    assert!(report.unlimited());
    let decision = NoNakedShortOptions::new("no-naked", false)
        .unwrap()
        .evaluate(&order, &state)
        .unwrap();
    assert_eq!(decision.action, RiskAction::Reject);
}

#[test]
fn ratio_write_is_unlimited() {
    let order = multi(&[
        (&osi("SPY", "241119", 'C', 490), Side::Buy, "1"),
        (&osi("SPY", "241119", 'C', 500), Side::Sell, "2"),
    ]);
    let report = option_collateral_report(&order, &context(), dec("100")).unwrap();
    assert!(report.unlimited());
    let decision = NoNakedShortOptions::new("no-naked", false)
        .unwrap()
        .evaluate(&order, &context())
        .unwrap();
    assert_eq!(decision.action, RiskAction::Reject);
}

#[test]
fn multiplier_parameter_scales_every_requirement() {
    let order = single(&osi("SPY", "241119", 'P', 450), Side::Sell, "1");
    let report = option_collateral_report(&order, &context(), dec("50")).unwrap();
    assert_eq!(report.total_collateral(), Some(dec("22500")));
    let allowed = OptionCollateralLimit {
        rule_id: "limit".to_string(),
        limit: dec("22500"),
        multiplier: dec("50"),
    }
    .evaluate(&order, &context())
    .unwrap();
    assert_eq!(allowed.action, RiskAction::Allow);
    let rejected = OptionCollateralLimit {
        rule_id: "limit".to_string(),
        limit: dec("22499.99"),
        multiplier: dec("50"),
    }
    .evaluate(&order, &context())
    .unwrap();
    assert_eq!(rejected.action, RiskAction::Reject);
    assert!(option_collateral_report(&order, &context(), dec("0")).is_err());
}

#[test]
fn reports_are_deterministic_and_per_contract() {
    let order = multi(&[
        (&osi("SPY", "241119", 'P', 450), Side::Sell, "1"),
        (&osi("SPY", "241119", 'C', 510), Side::Sell, "1"),
    ]);
    let first = option_collateral_report(&order, &context(), dec("100")).unwrap();
    let second = option_collateral_report(&order, &context(), dec("100")).unwrap();
    assert_eq!(first, second);
    assert_eq!(first.groups.len(), 1);
    let group = &first.groups[0];
    assert_eq!(group.root, "SPY");
    assert_eq!(group.expiration, "241119");
    let by_symbol: HashMap<&str, &super::ContractCollateral> = group
        .contracts
        .iter()
        .map(|record| (record.symbol.as_str(), record))
        .collect();
    let call = by_symbol[osi("SPY", "241119", 'C', 510).as_str()];
    assert_eq!(call.projected_contracts, dec("-1"));
    assert_eq!(call.naked_contracts, dec("1"));
    let put = by_symbol[osi("SPY", "241119", 'P', 450).as_str()];
    assert_eq!(put.naked_contracts, dec("1"));
}
