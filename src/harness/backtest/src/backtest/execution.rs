//! Owns strategy-scoped order actions, managed targets and execution-state views.
//! The venue owns matching; the portfolio owns booked money. This coordinator
//! never reconstructs positions from callbacks or selects a trading policy.
use super::{
    config::BacktestConfig,
    event_bus::EventBus,
    fixture::ExchangeSession,
    portfolio::PortfolioHub,
    risk::{evaluate_rule, ResolvedRiskRule, RiskAudit, RiskDecision},
    venue::SimulatedVenue,
};
use harness_trading::{program::*, replay_errors::BacktestError};
use num::Decimal;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

type ScopedOrders = (Arc<Vec<ExecutionOrder>>, Arc<Vec<ExecutionOrder>>);

pub(super) struct Execution {
    pub venue: SimulatedVenue,
    pub owners: BTreeMap<String, String>,
    pub counts: BTreeMap<String, u64>,
    pub risk_decisions: Vec<RiskDecision>,
    targets: BTreeMap<(String, String), Decimal>,
    sessions: Vec<ExchangeSession>,
    next_id: u64,
    trades: BTreeMap<String, harness_trading::fills::trades::TradeLedger>,
    cancellations: BTreeSet<String>,
    managed_orders: BTreeSet<String>,
    order_views: BTreeMap<String, ScopedOrders>,
}
impl Execution {
    pub fn new(sessions: Vec<ExchangeSession>) -> Self {
        Self {
            venue: SimulatedVenue::new(),
            owners: BTreeMap::new(),
            counts: BTreeMap::new(),
            risk_decisions: Vec::new(),
            targets: BTreeMap::new(),
            sessions,
            next_id: 0,
            trades: BTreeMap::new(),
            cancellations: BTreeSet::new(),
            managed_orders: BTreeSet::new(),
            order_views: BTreeMap::new(),
        }
    }
    pub fn orders(&self) -> Vec<ExecutionOrder> {
        self.venue.orders().as_ref().clone()
    }
    pub fn snapshot(
        &mut self,
        owner: &str,
        hub: &PortfolioHub,
        timestamp_ns: i64,
    ) -> Result<ExecutionSnapshot, BacktestError> {
        let mut snapshot = hub
            .book(owner)
            .ok_or_else(|| BacktestError::Configuration("missing strategy book".into()))?
            .execution_snapshot(timestamp_ns)?;
        let all_orders = self.venue.orders();
        let changed = self
            .order_views
            .get(owner)
            .is_none_or(|(previous, _)| !Arc::ptr_eq(previous, &all_orders));
        if changed {
            let owned = all_orders
                .iter()
                .filter(|order| {
                    self.owners
                        .get(&order.order_id)
                        .is_some_and(|id| id == owner)
                })
                .cloned()
                .collect();
            self.order_views
                .insert(owner.into(), (all_orders, Arc::new(owned)));
        }
        snapshot.orders = self.order_views[owner].1.clone();
        snapshot.closed_trades = self
            .trades
            .get(owner)
            .map_or_else(Default::default, |t| t.rows.clone());
        Ok(snapshot)
    }
    pub fn book_fill(&mut self, owner: &str, fill: &EngineFill) -> Result<(), BacktestError> {
        use harness_trading::fills::trades::TradeFill;
        use num::Decimal18;
        let convert = |value| Decimal18::from_decimal(value).map_err(arithmetic);
        self.trades
            .entry(owner.into())
            .or_default()
            .apply(&TradeFill {
                fill_id: fill.fill_id.clone(),
                symbol: fill.symbol.clone(),
                buy: fill.side == "buy",
                quantity: convert(fill.quantity)?,
                price: convert(fill.price)?,
                fee: convert(fill.fee)?,
                timestamp_ns: fill.timestamp_ns,
                sequence: fill.sequence,
            })
            .map_err(arithmetic)
    }
    #[allow(clippy::too_many_arguments)]
    pub fn actions(
        &mut self,
        actions: Vec<OrderAction>,
        owner: &str,
        config: &BacktestConfig,
        rules: &[ResolvedRiskRule],
        clock: i64,
        bus: &mut EventBus,
        hub: &PortfolioHub,
        prices: &BTreeMap<String, Decimal>,
        halted: &mut BTreeSet<String>,
    ) -> Result<(), BacktestError> {
        for action in actions {
            if halted.contains(owner) {
                break;
            }
            match action {
                OrderAction::Submit(order) => {
                    self.targets.remove(&(owner.into(), order.symbol.clone()));
                    self.submit(order, owner, config, rules, clock, bus, hub, prices, halted)?;
                }
                OrderAction::Cancel { order_id } => {
                    if self.owners.get(&order_id).is_none_or(|id| id != owner) {
                        return Err(BacktestError::StrategyExecution(
                            "cannot cancel an unknown or another strategy's order".into(),
                        ));
                    }
                    if let Some(symbol) = self.venue.order_symbol(&order_id) {
                        self.targets.remove(&(owner.into(), symbol.into()));
                    }
                    // An in-flight fill is already committed. A repeated cancel
                    // after that fill can cancel the remaining order quantity.
                    if !self.venue.cancel(&order_id) {
                        self.cancellations.insert(order_id);
                    }
                }
                OrderAction::TargetPosition { symbol, quantity } => {
                    if !config.symbols.contains(&symbol) {
                        return Err(BacktestError::StrategyExecution(
                            "target symbol is outside the configured universe".into(),
                        ));
                    }
                    self.targets
                        .insert((owner.into(), symbol.clone()), quantity);
                    self.reconcile(
                        owner, &symbol, config, rules, clock, bus, hub, prices, halted,
                    )?;
                }
            }
        }
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    pub fn reconcile_owner(
        &mut self,
        owner: &str,
        config: &BacktestConfig,
        rules: &[ResolvedRiskRule],
        clock: i64,
        bus: &mut EventBus,
        hub: &PortfolioHub,
        prices: &BTreeMap<String, Decimal>,
        halted: &mut BTreeSet<String>,
    ) -> Result<(), BacktestError> {
        let pending: Vec<_> = self
            .cancellations
            .iter()
            .filter(|id| self.owners.get(*id).is_some_and(|id| id == owner))
            .cloned()
            .collect();
        for id in pending {
            if self.venue.cancel(&id) {
                self.cancellations.remove(&id);
            }
        }
        let symbols: Vec<_> = self
            .targets
            .keys()
            .filter(|(id, _)| id == owner)
            .map(|(_, symbol)| symbol.clone())
            .collect();
        for symbol in symbols {
            self.reconcile(
                owner, &symbol, config, rules, clock, bus, hub, prices, halted,
            )?;
        }
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    fn reconcile(
        &mut self,
        owner: &str,
        symbol: &str,
        config: &BacktestConfig,
        rules: &[ResolvedRiskRule],
        clock: i64,
        bus: &mut EventBus,
        hub: &PortfolioHub,
        prices: &BTreeMap<String, Decimal>,
        halted: &mut BTreeSet<String>,
    ) -> Result<(), BacktestError> {
        if halted.contains(owner) {
            return Ok(());
        }
        let key = (owner.to_string(), symbol.to_string());
        let target = self.targets[&key];
        let quantity = hub
            .book(owner)
            .ok_or_else(|| BacktestError::Configuration("missing strategy book".into()))?
            .position_quantity(symbol);
        let working: Vec<_> = self
            .venue
            .working_orders(symbol)
            .into_iter()
            .filter(|order| {
                self.owners
                    .get(&order.order_id)
                    .is_some_and(|id| id == owner)
            })
            .collect();
        let mut projected = quantity;
        for order in &working {
            let signed = if order.side == "buy" {
                order.remaining_quantity
            } else {
                order.remaining_quantity.negated()
            };
            projected = projected.checked_add(signed).map_err(arithmetic)?;
        }
        // Only orders created to converge on a managed target can satisfy it.
        // A protective stop is conditional exposure, not a completed close.
        if projected == target
            && working
                .iter()
                .all(|order| self.managed_orders.contains(&order.order_id))
        {
            return Ok(());
        }
        let mut waiting = false;
        for order in working {
            if !self.venue.cancel(&order.order_id) {
                self.cancellations.insert(order.order_id.clone());
                waiting = true;
            }
        }
        if waiting {
            return Ok(());
        }
        let delta = target.checked_sub(quantity).map_err(arithmetic)?;
        if delta.is_zero() {
            return Ok(());
        }
        self.next_id += 1;
        let id = format!("managed:{owner}:{}", self.next_id);
        self.managed_orders.insert(id.clone());
        let order = EngineOrder {
            order_id: id.clone(),
            event_id: id,
            symbol: symbol.into(),
            side: if delta.is_positive() { "buy" } else { "sell" }.into(),
            quantity: delta.abs(),
            order_type: "market".into(),
            time_in_force: "gtc".into(),
            limit_price: None,
            stop_price: None,
            timestamp_ns: clock,
            sequence: 0,
        };
        self.submit(order, owner, config, rules, clock, bus, hub, prices, halted)?;
        if self
            .risk_decisions
            .last()
            .is_some_and(|d| d.action == "reject")
        {
            self.targets.remove(&key);
        }
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    fn submit(
        &mut self,
        order: EngineOrder,
        owner: &str,
        config: &BacktestConfig,
        rules: &[ResolvedRiskRule],
        clock: i64,
        bus: &mut EventBus,
        hub: &PortfolioHub,
        prices: &BTreeMap<String, Decimal>,
        halted: &mut BTreeSet<String>,
    ) -> Result<(), BacktestError> {
        if self.owners.contains_key(&order.order_id) {
            return Err(BacktestError::StrategyExecution(
                "order identity was already submitted".into(),
            ));
        }
        self.owners.insert(order.order_id.clone(), owner.into());
        let valid = !order.order_id.is_empty()
            && config.symbols.contains(&order.symbol)
            && matches!(order.side.as_str(), "buy" | "sell")
            && order.quantity > Decimal::ZERO
            && matches!(order.time_in_force.as_str(), "day" | "gtc" | "ioc")
            && match order.order_type.as_str() {
                "market" => order.limit_price.is_none() && order.stop_price.is_none(),
                "limit" => {
                    order.limit_price.is_some_and(|p| p > Decimal::ZERO)
                        && order.stop_price.is_none()
                }
                "stop" => {
                    order.stop_price.is_some_and(|p| p > Decimal::ZERO)
                        && order.limit_price.is_none()
                }
                "stop_limit" => {
                    order.limit_price.is_some_and(|p| p > Decimal::ZERO)
                        && order.stop_price.is_some_and(|p| p > Decimal::ZERO)
                }
                _ => false,
            };
        if !valid {
            self.venue.reject(order, "invalid order fields".into());
            return Ok(());
        }
        let expires = (order.time_in_force == "day")
            .then(|| {
                self.sessions
                    .iter()
                    .find(|session| clock < session.close_ns)
                    .map(|session| session.close_ns)
            })
            .flatten();
        if order.time_in_force == "day" && expires.is_none() {
            self.venue
                .reject(order, "no remaining exchange session for DAY order".into());
            return Ok(());
        }
        process_order(
            &order,
            owner,
            config,
            rules,
            clock,
            bus,
            &mut self.venue,
            hub,
            prices,
            &mut self.owners,
            &mut self.counts,
            &mut self.risk_decisions,
            halted,
            expires,
        )
    }
}
fn arithmetic(error: num::DecimalError) -> BacktestError {
    BacktestError::Arithmetic(format!("managed target: {error}"))
}

#[allow(clippy::too_many_arguments)]
fn process_order(
    order: &EngineOrder,
    instance_id: &str,
    configuration: &BacktestConfig,
    rules: &[ResolvedRiskRule],
    clock_ns: i64,
    bus: &mut EventBus,
    venue: &mut SimulatedVenue,
    hub: &PortfolioHub,
    prices: &BTreeMap<String, Decimal>,
    order_owners: &mut BTreeMap<String, String>,
    order_counts: &mut BTreeMap<String, u64>,
    risk_decisions: &mut Vec<RiskDecision>,
    halted_strategies: &mut BTreeSet<String>,
    expires_at_ns: Option<i64>,
) -> Result<(), BacktestError> {
    if halted_strategies.contains(instance_id) {
        return Ok(());
    }
    let snapshot = hub.snapshot();
    let current_quantity = snapshot
        .positions
        .iter()
        .find(|position| position.symbol == order.symbol)
        .map_or(Decimal::ZERO, |position| position.quantity);
    let mut audits = Vec::new();
    let mut reason = None;
    let mut action = "allow".to_string();
    let mut approved = order.clone();
    // A reduction restarts earlier rules so every approved order passes the full pipeline.
    let mut index = 0;
    let mut pass = 1;
    while index < rules.len() {
        let rule = &rules[index];
        let (rule_action, rule_reason, quantity) =
            evaluate_rule(rule, &approved, prices, current_quantity)?;
        audits.push(RiskAudit {
            sequence: audits.len() as u64,
            pass_number: pass,
            rule_id: rule.rule_id.clone(),
            action: rule_action.clone(),
            reason: rule_reason.clone(),
            halt: rule_action == "reject"
                && configuration.risk_pipeline.on_reject != "reject_order",
        });
        if rule_action == "reject" {
            action = "reject".to_string();
            reason = rule_reason;
            break;
        }
        if let Some(quantity) = quantity {
            approved.quantity = quantity;
            action = "reduce".into();
            reason = rule_reason;
            pass += 1;
            index = 0;
        } else {
            index += 1;
        }
    }
    risk_decisions.push(RiskDecision {
        action: action.clone(),
        order_id: order.order_id.clone(),
        strategy_id: instance_id.to_string(),
        reason: reason.clone(),
        halt_scope: if action == "reject" {
            harness_trading::risk::pipeline::OnReject::parse(&configuration.risk_pipeline.on_reject)
                .map_err(|e| BacktestError::Configuration(e.to_string()))?
                .halt_scope()
        } else {
            None
        },
        audits,
    });
    if action == "reject" {
        venue.reject(
            order.clone(),
            reason
                .clone()
                .unwrap_or_else(|| "risk rejected order".into()),
        );
        if configuration.risk_pipeline.on_reject == "halt_runtime" {
            return Err(BacktestError::BacktestAbort(
                reason.unwrap_or_else(|| "risk halted runtime".into()),
            ));
        }
        if configuration.risk_pipeline.on_reject == "halt_strategy" {
            halted_strategies.insert(instance_id.into());
        }
        return Ok(());
    }
    *order_counts.entry(instance_id.to_string()).or_insert(0) += 1;
    order_owners.insert(order.order_id.clone(), instance_id.to_string());
    venue.submit(approved, expires_at_ns, clock_ns, bus)
}
