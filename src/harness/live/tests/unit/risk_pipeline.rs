use std::collections::HashMap;

use num::Decimal18;

use super::{OnReject, RiskPipeline};
use crate::orders::{OrderRequest, OrderType, Side, SingleLegOrder, TimeInForce};
use crate::portfolio::hub::PortfolioHub;
use crate::risk::limits::{DailyLossHalt, MaxOrderSize};
use crate::risk::models::{RiskAction, RiskContext};
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn order(quantity: &str) -> OrderRequest {
    OrderRequest::Single(
        SingleLegOrder::new(
            "o1",
            Symbol::new("AAPL").unwrap(),
            Side::Buy,
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

fn context(daily_loss: &str) -> RiskContext {
    let hub = PortfolioHub::new("acct", vec![]).unwrap();
    RiskContext::new(
        hub.snapshot(),
        HashMap::new(),
        dec(daily_loss),
        0,
        Some("strat".to_string()),
    )
    .unwrap()
}

fn pipeline() -> RiskPipeline {
    RiskPipeline::new(
        vec![
            Box::new(MaxOrderSize::new("max-size", dec("5")).unwrap()),
            Box::new(DailyLossHalt::new("daily-loss", dec("10")).unwrap()),
        ],
        OnReject::RejectOrder,
    )
    .unwrap()
}

#[test]
fn oversized_order_is_reduced_and_rechecked() {
    let decision = pipeline().evaluate(&order("10"), &context("0")).unwrap();
    assert_eq!(decision.action, RiskAction::Reduce);
    assert!(decision.approved());
    assert!(decision.audits.iter().any(|audit| audit.recheck));
    assert_eq!(decision.order.as_single().unwrap().quantity, dec("5"));
}

#[test]
fn daily_loss_halt_rejects_with_scope() {
    let pipeline = RiskPipeline::new(
        vec![Box::new(
            DailyLossHalt::new("daily-loss", dec("10")).unwrap(),
        )],
        OnReject::HaltStrategy,
    )
    .unwrap();
    let decision = pipeline.evaluate(&order("1"), &context("10")).unwrap();
    assert!(decision.rejected());
    assert!(decision.halted());
    assert_eq!(decision.halt_scope.as_deref(), Some("halt_strategy"));
}
