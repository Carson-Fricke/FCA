use super::*;
use crate::backtest::errors::ONE_DAY_NS;
use crate::backtest::fixture::days_from_civil;

fn config() -> BacktestConfig {
    let start = days_from_civil(2024, 1, 1) * ONE_DAY_NS;
    let end = days_from_civil(2024, 1, 3) * ONE_DAY_NS;
    BacktestConfig::new("run", start, end, 7).expect("config")
}

#[test]
fn null_strategy_completes_with_no_orders() {
    let configuration = config();
    let options = BacktestOptions::default();
    let result = run_backtest(
        &configuration,
        None,
        &options,
        &mut NullStrategy,
        &mut ApproveAll,
        &mut ImmediateFill,
    )
    .expect("run");
    assert!(result.completed());
    assert_eq!(result.market_event_count, 36);
    assert_eq!(result.order_count, 0);
    assert_eq!(result.fill_count, 0);
    assert!(result.dispatch_hash.len() == 64);
}

#[test]
fn trade_strategy_fills_every_trade() {
    let configuration = config();
    let options = BacktestOptions::default();
    let mut strategy = TradeBuyStrategy::default();
    let result = run_backtest(
        &configuration,
        None,
        &options,
        &mut strategy,
        &mut ApproveAll,
        &mut ImmediateFill,
    )
    .expect("run");
    assert_eq!(strategy.emitted, 12);
    assert_eq!(result.order_count, 12);
    assert_eq!(result.fill_count, 12);
}

#[test]
fn engine_artifacts_are_deterministic() {
    let configuration = config();
    let options = BacktestOptions::default();
    let first = run_backtest(
        &configuration,
        None,
        &options,
        &mut TradeBuyStrategy::default(),
        &mut ApproveAll,
        &mut ImmediateFill,
    )
    .expect("first");
    let second = run_backtest(
        &configuration,
        None,
        &options,
        &mut TradeBuyStrategy::default(),
        &mut ApproveAll,
        &mut ImmediateFill,
    )
    .expect("second");
    assert_eq!(first.artifact_json(), second.artifact_json());
    assert_eq!(first.artifact_digest(), second.artifact_digest());
    assert_eq!(first.outcome_digest(), second.outcome_digest());
    assert_eq!(
        first.manifest.manifest_hash(),
        second.manifest.manifest_hash()
    );
}

#[test]
fn rejecting_risk_model_suppresses_orders() {
    struct RejectAll;
    impl RiskModel for RejectAll {
        fn evaluate(&mut self, _order: &OrderIntent, _event: &MarketEvent) -> RiskOutcome {
            RiskOutcome::Rejected("no".to_string())
        }
    }
    let configuration = config();
    let options = BacktestOptions::default();
    let result = run_backtest(
        &configuration,
        None,
        &options,
        &mut TradeBuyStrategy::default(),
        &mut RejectAll,
        &mut ImmediateFill,
    )
    .expect("run");
    assert_eq!(result.dispatched_count, 12);
    assert_eq!(result.order_count, 0);
}
