//! Public replay behavior of engine-owned accounting and managed order actions.
use backtest::backtest::{
    config::{BacktestConfig, RiskRuleConfig},
    engine_core::{run_native_engine, EngineOptions},
    fixture::{synthetic_fixture, SyntheticDataOptions},
    identity::build_default_manifest,
    outcome::BacktestOutcome,
    preflight::preflight_data,
};
use harness_trading::{
    program::*,
    replay_errors::{BacktestError, ONE_DAY_NS},
};
use num::Decimal;
use std::sync::{Arc, Mutex};

type Policy = Box<dyn FnMut(&EngineMarket, &ExecutionSnapshot) -> Vec<OrderAction>>;
struct Probe {
    state: Option<ExecutionSnapshot>,
    policy: Policy,
    observed: Arc<Mutex<Vec<ExecutionSnapshot>>>,
}
impl StrategyRuntime for Probe {
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        Some(vec![MarketSubscription {
            data_type: "quote".into(),
            symbols: vec!["SPY".into()],
            interval: None,
        }])
    }
    fn wants_execution_state(&self) -> bool {
        true
    }
    fn update_execution_state(&mut self, state: &ExecutionSnapshot) -> Result<(), BacktestError> {
        self.state = Some(state.clone());
        self.observed.lock().unwrap().push(state.clone());
        Ok(())
    }
    fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
        Ok(())
    }
    fn on_market(&mut self, _: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        Ok(Vec::new())
    }
    fn on_market_actions(
        &mut self,
        event: &EngineMarket,
    ) -> Result<Vec<OrderAction>, BacktestError> {
        Ok((self.policy)(
            event,
            self.state
                .as_ref()
                .expect("engine supplies state before callback"),
        ))
    }
}
fn run(policy: Policy, days: i64, reject: bool) -> (BacktestOutcome, Vec<ExecutionSnapshot>) {
    let mut config = BacktestConfig::new(
        "execution-api",
        19723 * ONE_DAY_NS,
        (19723 + days) * ONE_DAY_NS,
        7,
    )
    .unwrap();
    if reject {
        config.risk_pipeline.rules = vec![RiskRuleConfig {
            rule_id: "cap".into(),
            implementation: "MaxOrderNotional".into(),
            parameters: [(
                "limit".into(),
                harness_contracts::canonical_json::JsonValue::string("0"),
            )]
            .into(),
        }];
    }
    let observed = Arc::new(Mutex::new(Vec::new()));
    let program = Probe {
        state: None,
        policy,
        observed: observed.clone(),
    };
    config.data_subscriptions = program.subscriptions().unwrap();
    config.data_subscriptions_declared = true;
    let options = SyntheticDataOptions {
        quote_quantity: "4".parse().unwrap(),
        trade_quantity: "4".parse().unwrap(),
        ..Default::default()
    };
    let fixture = synthetic_fixture(&config, &options).unwrap();
    let preflight = preflight_data(&config, &fixture).unwrap();
    let manifest = build_default_manifest(&config, &fixture, &preflight).unwrap();
    let outcome = run_native_engine(
        &config,
        &fixture,
        &preflight,
        manifest,
        &EngineOptions::default(),
        vec![StrategyInstance {
            instance_id: "probe".into(),
            runtime: Box::new(program),
        }],
        &options,
    )
    .unwrap();
    let states = observed.lock().unwrap().clone();
    (outcome, states)
}
fn target(quantity: i128) -> OrderAction {
    OrderAction::TargetPosition {
        symbol: "SPY".into(),
        quantity: Decimal::from_i128(quantity),
    }
}
fn order(id: &str, kind: &str, tif: &str, qty: i128, limit: Option<i128>) -> OrderAction {
    OrderAction::Submit(EngineOrder {
        order_id: id.into(),
        symbol: "SPY".into(),
        side: "buy".into(),
        quantity: Decimal::from_i128(qty),
        order_type: kind.into(),
        time_in_force: tif.into(),
        limit_price: limit.map(Decimal::from_i128),
        stop_price: None,
        timestamp_ns: 19723 * ONE_DAY_NS,
        sequence: 0,
        event_id: id.into(),
    })
}

#[test]
fn closing_protected_inventory_cancels_the_stop_and_flattens_actual_partial_fills() {
    for direction in [1, -1] {
        for triggered in [false, true] {
            let mut stage = 0;
            let (outcome, states) = run(
                Box::new(move |_, state| {
                    let quantity = state
                        .positions
                        .iter()
                        .find(|position| position.symbol == "SPY")
                        .map_or(Decimal::ZERO, |position| position.quantity);
                    match stage {
                        0 if quantity == Decimal::from_i128(10 * direction) => {
                            stage = 1;
                            let OrderAction::Submit(mut stop) =
                                order("protective-stop", "stop", "gtc", 10, None)
                            else {
                                unreachable!()
                            };
                            stop.side = if direction > 0 { "sell" } else { "buy" }.into();
                            stop.stop_price =
                                Some(Decimal::from_i128(if (direction > 0) == triggered {
                                    10000
                                } else {
                                    1
                                }));
                            vec![OrderAction::Submit(stop)]
                        }
                        0 => vec![target(10 * direction)],
                        _ => {
                            stage = 2;
                            vec![target(0)]
                        }
                    }
                }),
                1,
                false,
            );
            assert!(outcome.completed());
            assert!(outcome
                .orders
                .iter()
                .any(|order| order.order_id == "protective-stop"
                    && order.status == "canceled"
                    && order.filled_quantity.is_zero() != triggered));
            assert_eq!(
                outcome.orders.len(),
                3,
                "repeated closes must join the managed exit"
            );
            assert!(states
                .last()
                .unwrap()
                .positions
                .iter()
                .all(|position| position.quantity.is_zero()));
            assert!(states.iter().any(|state| state
                .positions
                .iter()
                .any(|position| position.quantity.abs() == Decimal::from_i128(6))));
        }
    }
}

#[test]
fn managed_long_and_short_targets_handle_partial_fills_without_strategy_accounting() {
    for direction in [1, -1] {
        let mut closing = false;
        let (outcome, states) = run(
            Box::new(move |_, state| {
                if state
                    .positions
                    .iter()
                    .any(|p| p.quantity == Decimal::from_i128(10 * direction))
                {
                    closing = true;
                }
                vec![target(if closing { 0 } else { 10 * direction })]
            }),
            1,
            false,
        );
        assert!(outcome.completed());
        assert_eq!(
            outcome.orders.len(),
            2,
            "repeated target requests must not duplicate pending orders"
        );
        assert!(
            outcome.fills.len() >= 6,
            "entries and exits must actually partially fill"
        );
        let final_state = states.last().unwrap();
        assert!(final_state.positions.iter().all(|p| p.quantity.is_zero()));
        let pnl = final_state.cash - final_state.initial_cash;
        assert_eq!(final_state.realized_pnl, pnl);
        assert_eq!(final_state.unrealized_pnl, Some(Decimal::ZERO));
        assert!(
            pnl.abs() < Decimal::from_i128(100),
            "returned entry principal is never profit"
        );
        assert_eq!(final_state.gross_exposure, Some(Decimal::ZERO));
        assert!(states.iter().any(|s| s
            .positions
            .iter()
            .any(|p| p.quantity.abs() == Decimal::from_i128(4))));
    }
}

#[test]
fn a_managed_position_can_flip_direction_and_close_without_a_second_ledger() {
    let mut stage = 0;
    let (outcome, states) = run(
        Box::new(move |_, state| {
            let quantity = state
                .positions
                .iter()
                .find(|p| p.symbol == "SPY")
                .map_or(Decimal::ZERO, |p| p.quantity);
            if stage == 0 && quantity == Decimal::from_i128(10) {
                stage = 1;
            }
            if stage == 1 && quantity == Decimal::from_i128(-10) {
                stage = 2;
            }
            vec![target(match stage {
                0 => 10,
                1 => -10,
                _ => 0,
            })]
        }),
        1,
        false,
    );
    assert!(outcome.completed());
    assert_eq!(outcome.orders.len(), 3);
    assert!(states.iter().any(|s| s
        .positions
        .iter()
        .any(|p| p.quantity == Decimal::from_i128(-10))));
    let final_state = states.last().unwrap();
    assert!(final_state.positions.iter().all(|p| p.quantity.is_zero()));
    assert_eq!(
        final_state.realized_pnl,
        final_state.cash - final_state.initial_cash
    );
    let trade_pnl = final_state
        .closed_trades
        .iter()
        .try_fold(num::Decimal18::ZERO, |sum, trade| {
            sum.checked_add(trade.net_pnl)
        })
        .unwrap();
    assert_eq!(
        trade_pnl,
        num::Decimal18::from_decimal(final_state.realized_pnl).unwrap()
    );
}

#[test]
fn closing_an_inflight_entry_cancels_its_remainder_and_flattens_booked_inventory() {
    let mut calls = 0;
    let (outcome, states) = run(
        Box::new(move |_, _| {
            calls += 1;
            vec![target(if calls == 1 { 100 } else { 0 })]
        }),
        1,
        false,
    );
    assert!(outcome
        .orders
        .iter()
        .any(|o| o.status == "canceled" && o.filled_quantity > Decimal::ZERO));
    assert!(states
        .last()
        .unwrap()
        .positions
        .iter()
        .all(|p| p.quantity.is_zero()));
    assert!(outcome.orders.len() <= 3);
}

#[test]
fn a_later_explicit_order_does_not_erase_an_inflight_cancellation() {
    let mut once = false;
    let (outcome, _) = run(
        Box::new(move |_, _| {
            if once {
                return vec![];
            }
            once = true;
            vec![
                target(100),
                target(0),
                order("explicit", "limit", "gtc", 1, Some(1)),
            ]
        }),
        1,
        false,
    );
    assert!(outcome.completed());
    let entry = outcome
        .orders
        .iter()
        .find(|o| o.order_id.starts_with("managed:"))
        .unwrap();
    assert_eq!(entry.status, "canceled");
    assert_eq!(entry.filled_quantity, Decimal::from_i128(4));
    assert_eq!(
        outcome.fills.len(),
        1,
        "only the already scheduled fill books"
    );
}

#[test]
fn risk_rejection_is_visible_and_never_creates_inventory_or_fees() {
    let mut once = false;
    let (outcome, states) = run(
        Box::new(move |_, _| {
            if once {
                vec![]
            } else {
                once = true;
                vec![target(10)]
            }
        }),
        1,
        true,
    );
    assert!(outcome.fills.is_empty());
    let state = states.last().unwrap();
    assert_eq!(state.cash, state.initial_cash);
    assert_eq!(state.fees, Decimal::ZERO);
    assert_eq!(state.orders.len(), 1);
    assert_eq!(state.orders[0].status, "rejected");
    assert!(state.orders[0].reason.is_some());
}

#[test]
fn day_ioc_and_gtc_orders_have_real_lifecycle_without_inventing_fills() {
    let mut once = false;
    let (outcome, _) = run(
        Box::new(move |_, _| {
            if once {
                return vec![];
            }
            once = true;
            vec![
                order("day", "limit", "day", 10, Some(1)),
                order("gtc", "limit", "gtc", 10, Some(1)),
                order("ioc", "limit", "ioc", 10, Some(1)),
            ]
        }),
        2,
        false,
    );
    assert!(outcome.fills.is_empty());
    for (id, expected) in [("day", "expired"), ("gtc", "accepted"), ("ioc", "canceled")] {
        let actual = outcome.orders.iter().find(|o| o.order_id == id).unwrap();
        assert_eq!(actual.status, expected);
        assert_eq!(actual.filled_quantity, Decimal::ZERO);
    }
}

#[test]
fn ioc_partial_fill_cancels_remainder_instead_of_filling_later() {
    let mut once = false;
    let (outcome, _) = run(
        Box::new(move |_, _| {
            if once {
                vec![]
            } else {
                once = true;
                vec![order("ioc-partial", "market", "ioc", 10, None)]
            }
        }),
        1,
        false,
    );
    assert_eq!(outcome.fills.len(), 1);
    assert_eq!(outcome.fills[0].quantity, Decimal::from_i128(4));
    assert_eq!(outcome.orders[0].status, "canceled");
    assert_eq!(outcome.orders[0].remaining_quantity, Decimal::from_i128(6));
}

#[test]
fn simultaneous_day_expirations_retire_the_entire_live_batch_once() {
    let mut sent = false;
    let (outcome, _) = run(
        Box::new(move |_, _| {
            if sent {
                return vec![];
            }
            sent = true;
            (0..256)
                .map(|i| order(&format!("day-{i}"), "limit", "day", 1, Some(1)))
                .collect()
        }),
        2,
        false,
    );
    assert!(outcome.completed());
    assert_eq!(outcome.orders.len(), 256);
    assert!(outcome.orders.iter().all(|o| o.status == "expired"));
    assert!(outcome.fills.is_empty());
}
