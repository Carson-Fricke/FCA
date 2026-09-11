use num::Decimal18;

use super::{SubmitOutcome, TraderConfig, TraderCore};
use crate::error::LiveResult;
use crate::events::{MarketEvent, QuoteEvent};
use crate::orders::{OrderStatus, OrderType, Side, SingleLegOrder, TimeInForce};
use crate::risk::limits::{MaxOrderNotional, MaxOrderSize};
use crate::risk::models::{RiskContext, RiskRule};
use crate::risk::pipeline::OnReject;
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn config() -> TraderConfig {
    TraderConfig {
        account_id: "acct".into(),
        book_id: "book".into(),
        initial_cash: dec("100000"),
        venue: "sim".into(),
    }
}

fn order(quantity: &str) -> SingleLegOrder {
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
    .unwrap()
}

fn quote() -> MarketEvent {
    MarketEvent::Quote(
        QuoteEvent::new(
            Symbol::new("AAPL").unwrap(),
            dec("100"),
            dec("20"),
            dec("101"),
            dec("20"),
            None,
        )
        .unwrap(),
    )
}

#[test]
fn market_order_traverses_the_full_lifecycle() {
    let mut trader = TraderCore::new(
        config(),
        vec![Box::new(
            MaxOrderNotional::new("max-notional", dec("1000000")).unwrap(),
        )],
        OnReject::RejectOrder,
    )
    .unwrap();
    trader.on_market(quote()).unwrap();
    let outcome = trader.submit(order("10")).unwrap();
    assert!(matches!(outcome, SubmitOutcome::Approved(_)));
    let state = trader.state("o1").unwrap();
    assert_eq!(state.status, OrderStatus::Filled);
    assert_eq!(state.filled_quantity, dec("10"));
    assert_eq!(trader.position("AAPL").unwrap().quantity, dec("10"));
    assert_eq!(trader.cash().unwrap(), dec("98990"));
    assert_eq!(trader.reports().len(), 2);
}

#[test]
fn oversized_order_is_reduced_before_submission() {
    let mut trader = TraderCore::new(
        config(),
        vec![Box::new(MaxOrderSize::new("max-size", dec("5")).unwrap())],
        OnReject::RejectOrder,
    )
    .unwrap();
    trader.on_market(quote()).unwrap();
    trader.submit(order("10")).unwrap();
    let state = trader.state("o1").unwrap();
    assert_eq!(state.order.quantity, dec("5"));
    assert_eq!(state.filled_quantity, dec("5"));
    assert!(trader.risk_decisions()[0].reduced());
}

#[test]
fn kill_switch_rejection_halts_before_submission() {
    let mut switch = crate::risk::kill_switch::KillSwitch::new(None).unwrap();
    switch.trip(Some("test"), None).unwrap();
    let mut trader =
        TraderCore::new(config(), vec![Box::new(switch)], OnReject::HaltRuntime).unwrap();
    trader.on_market(quote()).unwrap();
    let outcome = trader.submit(order("1")).unwrap();
    match outcome {
        SubmitOutcome::Rejected(decision) => {
            assert!(decision.halted());
            assert_eq!(decision.halt_scope.as_deref(), Some("halt_runtime"));
        }
        SubmitOutcome::Approved(_) => panic!("kill switch must reject"),
    }
    assert!(trader.state("o1").is_err());
}

#[test]
fn risk_check_matches_submission_decision() {
    struct AlwaysReject;
    impl RiskRule for AlwaysReject {
        fn rule_id(&self) -> &str {
            "always-reject"
        }
        fn evaluate(
            &self,
            order: &crate::orders::OrderRequest,
            _context: &RiskContext,
        ) -> LiveResult<crate::risk::models::RuleDecision> {
            Ok(crate::risk::models::RuleDecision::reject(
                order.clone(),
                "rejected",
                false,
            ))
        }
    }
    let trader = TraderCore::new(
        config(),
        vec![Box::new(AlwaysReject)],
        OnReject::RejectOrder,
    )
    .unwrap();
    let check = trader.check(&order("1")).unwrap();
    assert!(check.rejected());
    assert!(!check.audits.is_empty() || check.reason.as_deref() == Some("rejected"));
}
