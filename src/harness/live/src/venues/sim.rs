//! Synchronous deterministic resting-order venue for top-of-book replay.
//!
//! Outcomes are collected on an internal queue rather than pushed to an
//! asynchronous bus; the runtime drains them in order. This keeps the kernel
//! deterministic and free of wall-clock and network access.

use std::collections::HashMap;

use num::Decimal18;

use crate::venues::plugin::FillPlugin;
use crate::venues::scripted::VenueOutcome;
use harness_trading::accounting::ExecutionReport;
use harness_trading::accounting::Fill;
use harness_trading::error::require_text;
use harness_trading::error::LiveError;
use harness_trading::error::LiveResult;
use harness_trading::events::MarketEvent;
use harness_trading::events::QuoteEvent;
use harness_trading::events::TradeEvent;
use harness_trading::fills::decision::FillDecision;
use harness_trading::fills::simple_touch::SimpleTouch;
use harness_trading::orders::ExecutionType;
use harness_trading::orders::OrderStatus;
use harness_trading::orders::OrderType;
use harness_trading::orders::Side;
use harness_trading::orders::SingleLegOrder;
use harness_trading::orders::TimeInForce;
use harness_trading::symbol::Symbol;

const ZERO: Decimal18 = Decimal18::ZERO;

fn is_terminal(status: OrderStatus) -> bool {
    matches!(
        status,
        OrderStatus::Filled
            | OrderStatus::Canceled
            | OrderStatus::Rejected
            | OrderStatus::Expired
            | OrderStatus::Replaced
    )
}

/// Immutable inspection snapshot of one venue-side order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SimOrderState {
    /// The immutable order.
    pub order: SingleLegOrder,
    /// Current status.
    pub status: OrderStatus,
    /// Cumulative filled quantity.
    pub filled_quantity: Decimal18,
    /// Remaining quantity.
    pub remaining_quantity: Decimal18,
    /// Whether a stop order is still armed.
    pub armed: bool,
    /// The stop or fill trigger snapshot.
    pub triggering_market: Option<MarketEvent>,
    /// The last decision price.
    pub decision_price: Option<Decimal18>,
    /// The last immutable fill decision.
    pub last_decision: Option<FillDecision>,
}

impl SimOrderState {
    /// The order identifier.
    pub fn order_id(&self) -> &str {
        &self.order.order_id
    }

    /// Whether the venue can still match or cancel the order.
    pub fn is_active(&self) -> bool {
        !is_terminal(self.status)
    }

    /// Whether a stop order has crossed its trigger.
    pub fn triggered(&self) -> bool {
        if matches!(
            self.order.order_type,
            OrderType::Stop | OrderType::StopLimit
        ) {
            !self.armed
        } else {
            true
        }
    }
}

#[derive(Clone, Debug)]
struct WorkingOrder {
    order: SingleLegOrder,
    status: OrderStatus,
    filled_quantity: Decimal18,
    filled_notional: Decimal18,
    armed: bool,
    stop_triggered: bool,
    triggering_market: Option<MarketEvent>,
    decision_price: Option<Decimal18>,
    last_decision: Option<FillDecision>,
    response_pending: bool,
}

impl WorkingOrder {
    fn new(order: SingleLegOrder, status: OrderStatus, armed: bool) -> Self {
        Self {
            order,
            status,
            filled_quantity: ZERO,
            filled_notional: ZERO,
            armed,
            stop_triggered: false,
            triggering_market: None,
            decision_price: None,
            last_decision: None,
            response_pending: false,
        }
    }

    fn remaining_quantity(&self) -> Decimal18 {
        self.order.quantity - self.filled_quantity
    }

    fn is_active(&self) -> bool {
        !is_terminal(self.status)
    }
}

/// Match single-leg orders against explicit quote/trade events.
pub struct SimulatedVenue {
    fill_model: Box<dyn FillPlugin>,
    venue: String,
    orders: HashMap<String, WorkingOrder>,
    order_ids: Vec<String>,
    used_order_ids: std::collections::HashSet<String>,
    latest_market: Option<MarketEvent>,
    latest_markets: HashMap<Symbol, MarketEvent>,
    decisions: Vec<FillDecision>,
    outcomes: Vec<VenueOutcome>,
    ordinal: usize,
    sequence: i64,
}

impl SimulatedVenue {
    /// Construct a venue using [`SimpleTouch`] by default.
    pub fn new(venue: &str) -> LiveResult<Self> {
        Ok(Self {
            fill_model: Box::new(SimpleTouch::new()),
            venue: require_text(venue, "venue")?,
            orders: HashMap::new(),
            order_ids: Vec::new(),
            used_order_ids: std::collections::HashSet::new(),
            latest_market: None,
            latest_markets: HashMap::new(),
            decisions: Vec::new(),
            outcomes: Vec::new(),
            ordinal: 0,
            sequence: -1,
        })
    }

    /// Construct a venue with a specific fill plugin.
    pub fn with_plugin(venue: &str, fill_model: Box<dyn FillPlugin>) -> LiveResult<Self> {
        let mut venue = Self::new(venue)?;
        venue.fill_model = fill_model;
        Ok(venue)
    }

    /// The last used venue id ordinal, or 0 before any identifier.
    pub fn id_ordinal(&self) -> usize {
        self.ordinal
    }

    /// The last used event sequence, or -1 before any event.
    pub fn event_sequence(&self) -> i64 {
        self.sequence
    }

    /// Continue id and sequence generation past restored runtime state.
    pub fn resume_from(&mut self, id_ordinal: usize, event_sequence: i64) {
        if id_ordinal > self.ordinal {
            self.ordinal = id_ordinal;
        }
        if event_sequence > self.sequence {
            self.sequence = event_sequence;
        }
    }

    /// The latest immutable market snapshot observed by the venue.
    pub fn latest_market(&self) -> Option<&MarketEvent> {
        self.latest_market.as_ref()
    }

    /// Fill decisions in deterministic matching order.
    pub fn decisions(&self) -> &[FillDecision] {
        &self.decisions
    }

    /// Outcomes scheduled so far, in scheduling order.
    pub fn outcomes(&self) -> &[VenueOutcome] {
        &self.outcomes
    }

    /// Drain the scheduled outcomes.
    pub fn drain_outcomes(&mut self) -> Vec<VenueOutcome> {
        std::mem::take(&mut self.outcomes)
    }

    /// Active order snapshots in submission order.
    pub fn active_orders(&self) -> Vec<SimOrderState> {
        self.order_ids
            .iter()
            .filter_map(|id| self.orders.get(id))
            .filter(|order| order.is_active())
            .map(snapshot)
            .collect()
    }

    /// Accept an order and schedule its acknowledgement and outcome.
    pub fn submit(&mut self, order: SingleLegOrder) -> LiveResult<()> {
        if self.used_order_ids.contains(&order.order_id) {
            return Err(LiveError::Venue(format!(
                "order_id {:?} was already used",
                order.order_id
            )));
        }
        let armed = matches!(order.order_type, OrderType::Stop | OrderType::StopLimit);
        let order_id = order.order_id.clone();
        let symbol = order.symbol.clone();
        let working = WorkingOrder::new(order, OrderStatus::Accepted, armed);
        self.orders.insert(order_id.clone(), working);
        self.order_ids.push(order_id.clone());
        self.used_order_ids.insert(order_id.clone());
        self.schedule_report(
            &order_id,
            ExecutionType::Acknowledged,
            OrderStatus::Accepted,
            ZERO,
            None,
        )?;
        if let Some(market) = self.latest_markets.get(&symbol).cloned() {
            let armed = self.orders.get(&order_id).is_some_and(|order| order.armed);
            if !armed {
                let decision = self.attempt(&order_id, &market, None)?;
                if decision.is_none() {
                    let tif = self
                        .orders
                        .get(&order_id)
                        .map(|order| order.order.time_in_force);
                    if tif == Some(TimeInForce::Ioc)
                        && self.is_matching_opportunity(&order_id, &market)?
                    {
                        self.schedule_expiry(&order_id, false)?;
                    }
                }
            }
        }
        Ok(())
    }

    /// Schedule cancellation confirmation for an active order.
    pub fn cancel(&mut self, order_id: &str) -> LiveResult<()> {
        let active = self
            .orders
            .get(order_id)
            .ok_or_else(|| LiveError::Venue(format!("unknown order {order_id:?}")))?;
        if !active.is_active() {
            return Err(LiveError::Venue(format!(
                "order {order_id:?} is not active"
            )));
        }
        if active.response_pending {
            return Err(LiveError::Venue(
                "cannot cancel while an execution outcome is pending".into(),
            ));
        }
        {
            let working = self.orders.get_mut(order_id).expect("checked");
            working.status = OrderStatus::Canceled;
            working.response_pending = true;
        }
        self.schedule_report(
            order_id,
            ExecutionType::Canceled,
            OrderStatus::Canceled,
            ZERO,
            None,
        )?;
        Ok(())
    }

    /// Atomically report replacement and begin the replacement order.
    pub fn replace(&mut self, order_id: &str, replacement: SingleLegOrder) -> LiveResult<()> {
        let (active, symbol, side, pending) = {
            let working = self
                .orders
                .get(order_id)
                .ok_or_else(|| LiveError::Venue(format!("unknown order {order_id:?}")))?;
            (
                working.is_active(),
                working.order.symbol.clone(),
                working.order.side,
                working.response_pending,
            )
        };
        if !active {
            return Err(LiveError::Venue(format!(
                "order {order_id:?} is not active"
            )));
        }
        if pending {
            return Err(LiveError::Venue(
                "cannot replace while an execution outcome is pending".into(),
            ));
        }
        if self.used_order_ids.contains(&replacement.order_id) {
            return Err(LiveError::Venue(format!(
                "order_id {:?} was already used",
                replacement.order_id
            )));
        }
        if replacement.symbol != symbol {
            return Err(LiveError::Venue("replacement cannot change symbol".into()));
        }
        if replacement.side != side {
            return Err(LiveError::Venue("replacement cannot change side".into()));
        }

        {
            let working = self.orders.get_mut(order_id).expect("checked");
            working.status = OrderStatus::Replaced;
            working.response_pending = true;
        }
        self.used_order_ids.insert(replacement.order_id.clone());
        self.schedule_report(
            order_id,
            ExecutionType::Replaced,
            OrderStatus::Replaced,
            ZERO,
            None,
        )?;

        let replacement_id = replacement.order_id.clone();
        let replacement_symbol = replacement.symbol.clone();
        let armed = matches!(
            replacement.order_type,
            OrderType::Stop | OrderType::StopLimit
        );
        let working = WorkingOrder::new(replacement, OrderStatus::Accepted, armed);
        self.orders.insert(replacement_id.clone(), working);
        self.order_ids.push(replacement_id.clone());
        self.schedule_report(
            &replacement_id,
            ExecutionType::Acknowledged,
            OrderStatus::Accepted,
            ZERO,
            None,
        )?;
        if let Some(market) = self.latest_markets.get(&replacement_symbol).cloned() {
            let armed = self
                .orders
                .get(&replacement_id)
                .is_some_and(|order| order.armed);
            if !armed {
                let decision = self.attempt(&replacement_id, &market, None)?;
                if decision.is_none() {
                    let tif = self
                        .orders
                        .get(&replacement_id)
                        .map(|order| order.order.time_in_force);
                    if tif == Some(TimeInForce::Ioc)
                        && self.is_matching_opportunity(&replacement_id, &market)?
                    {
                        self.schedule_expiry(&replacement_id, false)?;
                    }
                }
            }
        }
        Ok(())
    }

    /// Validate the protocol command; positions belong to the accounting hub.
    pub fn request_position_snapshot(&self, account_id: &str) -> LiveResult<()> {
        require_text(account_id, "account_id")?;
        Ok(())
    }

    /// Return an immutable venue-side state snapshot.
    pub fn state(&self, order_id: &str) -> LiveResult<SimOrderState> {
        let working = self
            .orders
            .get(order_id)
            .ok_or_else(|| LiveError::Venue(format!("unknown order {order_id:?}")))?;
        Ok(snapshot(working))
    }

    /// Return all decisions made for one order.
    pub fn decisions_for(&self, order_id: &str) -> LiveResult<Vec<FillDecision>> {
        if !self.orders.contains_key(order_id) {
            return Err(LiveError::Venue(format!("unknown order {order_id:?}")));
        }
        Ok(self
            .decisions
            .iter()
            .filter(|decision| decision.order_id == order_id)
            .cloned()
            .collect())
    }

    /// Feed one market event into the matching engine.
    pub fn on_market(&mut self, event: MarketEvent) -> LiveResult<()> {
        self.latest_market = Some(event.clone());
        let symbol = event.symbol().clone();
        self.latest_markets.insert(symbol.clone(), event.clone());
        let (buy_available, sell_available) = match &event {
            MarketEvent::Trade(trade) => (trade.quantity, trade.quantity),
            MarketEvent::Quote(quote) => (quote.ask_quantity, quote.bid_quantity),
        };
        let order_ids = self.order_ids.clone();
        for order_id in order_ids {
            let (active, pending, order_symbol, side, tif) = {
                let Some(working) = self.orders.get(&order_id) else {
                    continue;
                };
                (
                    working.is_active(),
                    working.response_pending,
                    working.order.symbol.clone(),
                    working.order.side,
                    working.order.time_in_force,
                )
            };
            if !active || pending || order_symbol != symbol {
                continue;
            }
            let mut available = if side == Side::Buy {
                buy_available
            } else {
                sell_available
            };
            let decision = self.attempt(&order_id, &event, Some(available))?;
            match decision {
                Some(decision) => available -= decision.quantity,
                None => {
                    if tif == TimeInForce::Ioc && self.is_matching_opportunity(&order_id, &event)? {
                        self.schedule_expiry(&order_id, false)?;
                    }
                }
            }
        }
        Ok(())
    }

    fn attempt(
        &mut self,
        order_id: &str,
        market: &MarketEvent,
        available: Option<Decimal18>,
    ) -> LiveResult<Option<FillDecision>> {
        let (remaining, is_stop, mut stop_triggered, mut triggering_market) = {
            let Some(working) = self.orders.get(order_id) else {
                return Ok(None);
            };
            (
                working.remaining_quantity(),
                matches!(
                    working.order.order_type,
                    OrderType::Stop | OrderType::StopLimit
                ),
                working.stop_triggered,
                working.triggering_market.clone(),
            )
        };
        if !remaining.is_positive() {
            return Ok(None);
        }
        if is_stop && !stop_triggered {
            if !self
                .fill_model
                .is_triggered(&self.orders.get(order_id).expect("present").order, market)?
            {
                return Ok(None);
            }
            stop_triggered = true;
            triggering_market = Some(market.clone());
            if let Some(working) = self.orders.get_mut(order_id) {
                working.stop_triggered = true;
                working.armed = false;
                working.triggering_market = Some(market.clone());
            }
        }

        let order = self.orders.get(order_id).expect("present").order.clone();
        let Some(mut decision) =
            self.fill_model
                .decide(&order, market, Some(remaining), available, stop_triggered)?
        else {
            return Ok(None);
        };
        if is_stop {
            if let Some(trigger) = &triggering_market {
                if decision.triggering_market != *trigger {
                    decision.triggering_market = trigger.clone();
                }
            }
        }
        {
            let working = self.orders.get_mut(order_id).expect("present");
            working.triggering_market = Some(decision.triggering_market.clone());
            working.decision_price = Some(decision.decision_price);
            working.last_decision = Some(decision.clone());
        }
        self.decisions.push(decision.clone());
        self.schedule_fill(order_id, &decision)?;
        Ok(Some(decision))
    }

    fn schedule_fill(&mut self, order_id: &str, decision: &FillDecision) -> LiveResult<()> {
        if decision.order_id != order_id {
            return Err(LiveError::Venue(
                "fill decision order_id does not match working order".into(),
            ));
        }
        let (status, order, last_quantity, last_price) = {
            let working = self
                .orders
                .get_mut(order_id)
                .ok_or_else(|| LiveError::Venue(format!("unknown order {order_id:?}")))?;
            if decision.quantity > working.remaining_quantity() {
                return Err(LiveError::Venue(
                    "fill decision exceeds the order remainder".into(),
                ));
            }
            working.filled_quantity += decision.quantity;
            working.filled_notional += decision.quantity * decision.price;
            working.status = if working.remaining_quantity().is_zero() {
                OrderStatus::Filled
            } else {
                OrderStatus::PartiallyFilled
            };
            working.response_pending = true;
            (
                working.status,
                working.order.clone(),
                decision.quantity,
                decision.price,
            )
        };
        let execution_type = if status == OrderStatus::Filled {
            ExecutionType::Fill
        } else {
            ExecutionType::PartialFill
        };
        self.schedule_report(
            order_id,
            execution_type,
            status,
            last_quantity,
            Some(last_price),
        )?;
        let fill_id = self.next_id("fill");
        self.outcomes.push(VenueOutcome::Fill(Fill::new(
            &fill_id,
            order_id,
            order.symbol,
            order.side,
            last_quantity,
            last_price,
            ZERO,
            Some(decision.liquidity.clone()),
        )?));
        if status == OrderStatus::PartiallyFilled && order.time_in_force == TimeInForce::Ioc {
            self.schedule_expiry(order_id, true)?;
        }
        if let Some(working) = self.orders.get_mut(order_id) {
            working.response_pending = false;
        }
        Ok(())
    }

    fn schedule_expiry(&mut self, order_id: &str, allow_pending: bool) -> LiveResult<()> {
        let (active, pending) = {
            let Some(working) = self.orders.get(order_id) else {
                return Ok(());
            };
            (working.is_active(), working.response_pending)
        };
        if !active || (pending && !allow_pending) {
            return Ok(());
        }
        if let Some(working) = self.orders.get_mut(order_id) {
            working.status = OrderStatus::Expired;
            working.response_pending = true;
        }
        self.schedule_report(
            order_id,
            ExecutionType::Expired,
            OrderStatus::Expired,
            ZERO,
            None,
        )?;
        if let Some(working) = self.orders.get_mut(order_id) {
            working.response_pending = false;
        }
        Ok(())
    }

    fn schedule_report(
        &mut self,
        order_id: &str,
        execution_type: ExecutionType,
        status: OrderStatus,
        last_fill_quantity: Decimal18,
        last_fill_price: Option<Decimal18>,
    ) -> LiveResult<()> {
        let (order, filled, remaining, notional, last_decision) = {
            let working = self
                .orders
                .get(order_id)
                .ok_or_else(|| LiveError::Venue(format!("unknown order {order_id:?}")))?;
            (
                working.order.clone(),
                working.filled_quantity,
                working.remaining_quantity(),
                working.filled_notional,
                working.last_decision.clone(),
            )
        };
        let average = if filled.is_zero() {
            None
        } else {
            Some(notional / filled)
        };
        let text = last_decision.map(|decision| {
            format!(
                "decision_price={};triggering_market={:?}",
                decision.decision_price, decision.triggering_market
            )
        });
        let report_id = self.next_id("report");
        let report = ExecutionReport::new(
            &report_id,
            order_id,
            status,
            execution_type,
            order.quantity,
            filled,
            remaining,
            average,
            last_fill_quantity,
            last_fill_price,
            text,
        )?;
        self.outcomes.push(VenueOutcome::Report(report));
        Ok(())
    }

    fn is_matching_opportunity(&self, order_id: &str, market: &MarketEvent) -> LiveResult<bool> {
        let Some(working) = self.orders.get(order_id) else {
            return Ok(false);
        };
        if matches!(
            working.order.order_type,
            OrderType::Stop | OrderType::StopLimit
        ) {
            if working.stop_triggered {
                return Ok(true);
            }
            return self.fill_model.is_triggered(&working.order, market);
        }
        Ok(true)
    }

    fn next_id(&mut self, kind: &str) -> String {
        self.ordinal += 1;
        format!("{}-{}-{}", self.venue, kind, self.ordinal)
    }
}

fn snapshot(working: &WorkingOrder) -> SimOrderState {
    SimOrderState {
        order: working.order.clone(),
        status: working.status,
        filled_quantity: working.filled_quantity,
        remaining_quantity: working.remaining_quantity(),
        armed: working.armed,
        triggering_market: working.triggering_market.clone(),
        decision_price: working.decision_price,
        last_decision: working.last_decision.clone(),
    }
}

impl Default for SimulatedVenue {
    fn default() -> Self {
        Self::new("sim").expect("default venue name is valid")
    }
}

/// Convenience alias used by venue tests.
pub fn simple_quote(symbol: &str, bid: &str, ask: &str, quantity: &str) -> LiveResult<MarketEvent> {
    Ok(MarketEvent::Quote(QuoteEvent::new(
        Symbol::new(symbol)?,
        bid.parse().map_err(|_| LiveError::Venue("bid".into()))?,
        quantity
            .parse()
            .map_err(|_| LiveError::Venue("qty".into()))?,
        ask.parse().map_err(|_| LiveError::Venue("ask".into()))?,
        quantity
            .parse()
            .map_err(|_| LiveError::Venue("qty".into()))?,
        None,
    )?))
}

/// Convenience alias for a trade event.
pub fn simple_trade(symbol: &str, price: &str, quantity: &str) -> LiveResult<MarketEvent> {
    Ok(MarketEvent::Trade(TradeEvent::new(
        Symbol::new(symbol)?,
        price
            .parse()
            .map_err(|_| LiveError::Venue("price".into()))?,
        quantity
            .parse()
            .map_err(|_| LiveError::Venue("qty".into()))?,
        None,
        None,
    )?))
}
