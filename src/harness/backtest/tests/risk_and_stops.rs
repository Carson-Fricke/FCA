//! Replay-boundary regressions for stop activation, risk reductions and halt scope.
use backtest::backtest::{
    config::{BacktestConfig, RiskRuleConfig},
    engine_core::{run_native_engine, EngineOptions},
    fixture::{synthetic_fixture, SyntheticDataOptions},
    identity::build_default_manifest,
    outcome::BacktestOutcome,
    preflight::preflight_data,
};
use harness_contracts::canonical_json::JsonValue;
use harness_trading::{
    program::*,
    replay_errors::{BacktestError, BacktestStatus, ONE_DAY_NS},
};
use num::Decimal;
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};
struct Program {
    calls: Arc<AtomicUsize>,
    stops: Arc<AtomicUsize>,
    kind: &'static str,
}
impl StrategyRuntime for Program {
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        Some(vec![MarketSubscription {
            data_type: "quote".into(),
            symbols: vec!["SPY".into()],
            interval: None,
        }])
    }
    fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
        Ok(())
    }
    fn on_market(&mut self, e: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        let n = self.calls.fetch_add(1, Ordering::SeqCst);
        if self.kind != "market" && n > 0 {
            return Ok(vec![]);
        }
        Ok(vec![EngineOrder {
            order_id: format!("o-{n}"),
            symbol: "SPY".into(),
            side: "buy".into(),
            quantity: Decimal::from_i128(10),
            order_type: self.kind.into(),
            time_in_force: "gtc".into(),
            limit_price: None,
            stop_price: if self.kind == "stop" {
                Some(Decimal::from_i128(9999))
            } else {
                None
            },
            timestamp_ns: e.timestamp_ns(),
            sequence: 100 + n as u64,
            event_id: format!("i-{n}"),
        }])
    }
    fn on_stop(&mut self) -> Result<(), BacktestError> {
        self.stops.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}
fn run(policy: &str, limit: Option<&str>, kind: &'static str) -> (BacktestOutcome, usize, usize) {
    let mut c = BacktestConfig::new("policies", 19723 * ONE_DAY_NS, 19724 * ONE_DAY_NS, 7).unwrap();
    c.risk_pipeline.on_reject = policy.into();
    if let Some(limit) = limit {
        c.risk_pipeline.rules = vec![RiskRuleConfig {
            rule_id: "cap".into(),
            implementation: "MaxOrderNotional".into(),
            parameters: [("limit".into(), JsonValue::String(limit.into()))].into(),
        }];
    }
    let calls = Arc::new(AtomicUsize::new(0));
    let stops = Arc::new(AtomicUsize::new(0));
    let p = Program {
        calls: calls.clone(),
        stops: stops.clone(),
        kind,
    };
    c.data_subscriptions = p.subscriptions().unwrap();
    c.data_subscriptions_declared = true;
    let options = SyntheticDataOptions::default();
    let fixture = synthetic_fixture(&c, &options).unwrap();
    let preflight = preflight_data(&c, &fixture).unwrap();
    let manifest = build_default_manifest(&c, &fixture, &preflight).unwrap();
    let outcome = run_native_engine(
        &c,
        &fixture,
        &preflight,
        manifest,
        &EngineOptions::default(),
        vec![StrategyInstance {
            instance_id: "program".into(),
            runtime: Box::new(p),
        }],
        &options,
    )
    .unwrap();
    (
        outcome,
        calls.load(Ordering::SeqCst),
        stops.load(Ordering::SeqCst),
    )
}
#[test]
fn stop_order_waits_for_activation_in_replay() {
    let (o, _, stops) = run("reject_order", None, "stop");
    assert_eq!(o.order_count, 1);
    assert!(o.fills.is_empty());
    assert_eq!(stops, 1);
}
#[test]
fn risk_rejection_obeys_each_halt_scope_and_runs_teardown() {
    let (rejected, calls, stops) = run("reject_order", Some("0"), "market");
    assert!(calls > 1);
    assert_eq!(rejected.risk_decisions.len(), calls);
    assert_eq!(stops, 1);
    let (halted, calls, stops) = run("halt_strategy", Some("0"), "market");
    assert_eq!(calls, 1);
    assert_eq!(halted.halted_strategies, vec!["program"]);
    assert_eq!(stops, 1);
    let (halted, calls, stops) = run("halt_runtime", Some("0"), "market");
    assert_eq!(calls, 1);
    assert_eq!(halted.status, BacktestStatus::Aborted);
    assert_eq!(stops, 1);
    assert!(halted.reason.is_some());
}
#[test]
fn replay_submits_the_reduced_order_after_rechecking_the_pipeline() {
    let (o, _, _) = run("reject_order", Some("500"), "market");
    assert!(!o.fills.is_empty());
    assert!(o.risk_decisions.iter().all(|d| d.action == "reduce"));
    assert!(o
        .fills
        .iter()
        .all(|f| f.quantity.cmp_value(Decimal::from_i128(10)).is_lt()));
}
#[test]
fn stop_limit_activation_is_sticky_until_a_later_limit_touch() {
    use harness_trading::{
        matching::touch,
        orders::{OrderType, Side},
    };
    let d = Decimal::from_i128;
    let (a, p) = touch(
        Side::Buy,
        OrderType::StopLimit,
        Some(d(95)),
        Some(d(100)),
        d(90),
        true,
        false,
    );
    assert!(!a);
    assert!(p.is_none());
    let (a, p) = touch(
        Side::Buy,
        OrderType::StopLimit,
        Some(d(95)),
        Some(d(100)),
        d(110),
        true,
        a,
    );
    assert!(a);
    assert!(p.is_none());
    let (a, p) = touch(
        Side::Buy,
        OrderType::StopLimit,
        Some(d(95)),
        Some(d(100)),
        d(90),
        true,
        a,
    );
    assert!(a);
    assert_eq!(p, Some(d(90)));
}
