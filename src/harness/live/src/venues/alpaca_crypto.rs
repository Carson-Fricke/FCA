//! Deterministic fractional crypto execution venue for Alpaca accounts.
//!
//! Crypto trades continuously, so commands never consult a session calendar.
//! Quantities stay exact through the full submit/cancel/replace path, resting
//! orders match later market events as maker fills, and submit-time fills
//! against the already visible book are taker fills. Every fill is reconciled
//! against the locally configured maker-taker model and the venue fee schedule
//! before any outcome is scheduled, and a disagreement surfaces as an error
//! rather than a silently recorded fee.
//!
//! Like [`SimulatedVenue`](crate::venues::sim::SimulatedVenue), the venue owns
//! no clock, network, or random resource: the runtime feeds market events and
//! drains scheduled outcomes in order.

use std::collections::{HashMap, HashSet};

use num::Decimal18;

use crate::accounting::{ExecutionReport, Fill};
use crate::error::{require_text, LiveError, LiveResult};
use crate::events::MarketEvent;
use crate::fills::decision::FillDecision;
use crate::fills::fees::{CryptoMakerTakerFeeModel, FeeRounding, LiquidityRole};
use crate::fills::simple_touch::SimpleTouch;
use crate::orders::{ExecutionType, OrderStatus, OrderType, Side, SingleLegOrder, TimeInForce};
use crate::symbol::Symbol;
use crate::venues::plugin::FillPlugin;
use crate::venues::scripted::VenueOutcome;

const BASIS_POINTS: i64 = 10_000;
const ZERO: Decimal18 = Decimal18::ZERO;

/// Return whether a canonical symbol is a `BASE/QUOTE` crypto pair.
pub fn is_crypto_pair(symbol: &str) -> bool {
    let mut parts = symbol.splitn(2, '/');
    match (parts.next(), parts.next()) {
        (Some(base), Some(quote)) => !base.is_empty() && !quote.is_empty() && !quote.contains('/'),
        _ => false,
    }
}

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

fn role_name(role: LiquidityRole) -> &'static str {
    match role {
        LiquidityRole::Maker => "maker",
        LiquidityRole::Taker => "taker",
    }
}

/// A venue-reported maker-taker crypto fee schedule fixture.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlpacaCryptoFeeSchedule {
    /// Schedule identifier.
    pub schedule_id: String,
    /// ISO currency code.
    pub currency: String,
    /// Maker rate in basis points.
    pub maker_rate_bps: Decimal18,
    /// Taker rate in basis points.
    pub taker_rate_bps: Decimal18,
    /// Rounding rule.
    pub rounding: FeeRounding,
}

impl AlpacaCryptoFeeSchedule {
    /// Construct a validated schedule.
    pub fn new(
        schedule_id: &str,
        currency: &str,
        maker_rate_bps: Decimal18,
        taker_rate_bps: Decimal18,
        rounding: FeeRounding,
    ) -> LiveResult<Self> {
        if maker_rate_bps.is_negative() || taker_rate_bps.is_negative() {
            return Err(LiveError::Venue(
                "crypto fee rates must not be negative".into(),
            ));
        }
        if currency.len() != 3 || !currency.bytes().all(|byte| byte.is_ascii_uppercase()) {
            return Err(LiveError::Venue(
                "currency must be a three-letter uppercase code".into(),
            ));
        }
        Ok(Self {
            schedule_id: require_text(schedule_id, "schedule_id")?,
            currency: currency.to_string(),
            maker_rate_bps,
            taker_rate_bps,
            rounding,
        })
    }

    /// Return the schedule rate in basis points for one liquidity role.
    pub fn rate_for(&self, liquidity: LiquidityRole) -> Decimal18 {
        match liquidity {
            LiquidityRole::Maker => self.maker_rate_bps,
            LiquidityRole::Taker => self.taker_rate_bps,
        }
    }

    /// Return the venue-reported charge for one fill, exactly rounded.
    pub fn charge(
        &self,
        quantity: Decimal18,
        price: Decimal18,
        liquidity: LiquidityRole,
    ) -> LiveResult<Decimal18> {
        if !quantity.is_positive() || !price.is_positive() {
            return Err(LiveError::Venue(
                "quantity and price must be positive".into(),
            ));
        }
        let notional = quantity * price;
        Ok(self
            .rounding
            .apply(notional * self.rate_for(liquidity) / Decimal18::from_i64(BASIS_POINTS)))
    }
}

/// One reconciled local fee record for a single fill.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CryptoFeeLedgerEntry {
    /// The fill identifier.
    pub fill_id: String,
    /// The order identifier.
    pub order_id: String,
    /// The pair symbol.
    pub symbol: Symbol,
    /// Buy or sell.
    pub side: Side,
    /// Exact quantity.
    pub quantity: Decimal18,
    /// Exact price.
    pub price: Decimal18,
    /// The resolved liquidity role.
    pub liquidity: LiquidityRole,
    /// The locally configured fee.
    pub local_fee: Decimal18,
    /// The venue-reported fee.
    pub venue_fee: Decimal18,
}

/// Immutable inspection snapshot of one venue-side crypto order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CryptoOrderState {
    /// The immutable order.
    pub order: SingleLegOrder,
    /// Current status.
    pub status: OrderStatus,
    /// Cumulative filled quantity.
    pub filled_quantity: Decimal18,
    /// Remaining quantity.
    pub remaining_quantity: Decimal18,
    /// Cumulative reconciled fees.
    pub fees_paid: Decimal18,
    /// The last immutable fill decision.
    pub last_decision: Option<FillDecision>,
}

impl CryptoOrderState {
    /// The order identifier.
    pub fn order_id(&self) -> &str {
        &self.order.order_id
    }

    /// Whether the venue can still match or cancel the order.
    pub fn is_active(&self) -> bool {
        !is_terminal(self.status)
    }
}

#[derive(Clone, Debug)]
struct WorkingOrder {
    order: SingleLegOrder,
    status: OrderStatus,
    filled_quantity: Decimal18,
    filled_notional: Decimal18,
    fees_paid: Decimal18,
    last_decision: Option<FillDecision>,
    response_pending: bool,
}

impl WorkingOrder {
    fn new(order: SingleLegOrder, status: OrderStatus) -> Self {
        Self {
            order,
            status,
            filled_quantity: ZERO,
            filled_notional: ZERO,
            fees_paid: ZERO,
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

/// The deterministic crypto venue with matching and fee reconciliation.
pub struct AlpacaCryptoVenue {
    fill_model: Box<dyn FillPlugin>,
    venue: String,
    local: CryptoMakerTakerFeeModel,
    schedule: AlpacaCryptoFeeSchedule,
    ledger: Vec<CryptoFeeLedgerEntry>,
    orders: HashMap<String, WorkingOrder>,
    order_ids: Vec<String>,
    used_order_ids: HashSet<String>,
    latest_market: Option<MarketEvent>,
    latest_markets: HashMap<Symbol, MarketEvent>,
    decisions: Vec<FillDecision>,
    outcomes: Vec<VenueOutcome>,
    ordinal: usize,
}

impl AlpacaCryptoVenue {
    /// Construct a venue using [`SimpleTouch`] by default.
    pub fn new(local: CryptoMakerTakerFeeModel, schedule: AlpacaCryptoFeeSchedule) -> Self {
        Self::with_plugin(local, schedule, Box::new(SimpleTouch::new()))
    }

    /// Construct a venue with a specific fill plugin.
    pub fn with_plugin(
        local: CryptoMakerTakerFeeModel,
        schedule: AlpacaCryptoFeeSchedule,
        fill_model: Box<dyn FillPlugin>,
    ) -> Self {
        Self {
            fill_model,
            venue: "alpaca-crypto".to_string(),
            local,
            schedule,
            ledger: Vec::new(),
            orders: HashMap::new(),
            order_ids: Vec::new(),
            used_order_ids: HashSet::new(),
            latest_market: None,
            latest_markets: HashMap::new(),
            decisions: Vec::new(),
            outcomes: Vec::new(),
            ordinal: 0,
        }
    }

    /// The venue identifier embedded in generated ids.
    pub fn venue(&self) -> &str {
        &self.venue
    }

    /// The local maker-taker fee model used for reconciliation.
    pub fn fee_model(&self) -> &CryptoMakerTakerFeeModel {
        &self.local
    }

    /// The venue-reported fee schedule used for reconciliation.
    pub fn fee_schedule(&self) -> &AlpacaCryptoFeeSchedule {
        &self.schedule
    }

    /// The latest immutable market snapshot observed by the venue.
    pub fn latest_market(&self) -> Option<&MarketEvent> {
        self.latest_market.as_ref()
    }

    /// Fill decisions in deterministic matching order.
    pub fn decisions(&self) -> &[FillDecision] {
        &self.decisions
    }

    /// The reconciled fee ledger.
    pub fn fee_ledger(&self) -> &[CryptoFeeLedgerEntry] {
        &self.ledger
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
    pub fn active_orders(&self) -> Vec<CryptoOrderState> {
        self.order_ids
            .iter()
            .filter_map(|id| self.orders.get(id))
            .filter(|order| order.is_active())
            .map(snapshot)
            .collect()
    }

    /// Accept a fractional order and schedule its venue outcomes.
    pub fn submit(&mut self, order: SingleLegOrder) -> LiveResult<()> {
        validate_crypto_order(&order)?;
        if self.used_order_ids.contains(&order.order_id) {
            return Err(LiveError::Venue(format!(
                "order_id {:?} was already used",
                order.order_id
            )));
        }
        let order_id = order.order_id.clone();
        let symbol = order.symbol.clone();
        self.orders.insert(
            order_id.clone(),
            WorkingOrder::new(order, OrderStatus::Accepted),
        );
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
            let decision = self.attempt(&order_id, &market, LiquidityRole::Taker, None)?;
            if decision.is_none() {
                let tif = self
                    .orders
                    .get(&order_id)
                    .map(|order| order.order.time_in_force);
                if tif == Some(TimeInForce::Ioc) {
                    self.schedule_expiry(&order_id, false)?;
                }
            }
        }
        Ok(())
    }

    /// Schedule cancellation confirmation for an active, non-pending order.
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
        validate_crypto_order(&replacement)?;
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
        self.orders.insert(
            replacement_id.clone(),
            WorkingOrder::new(replacement, OrderStatus::Accepted),
        );
        self.order_ids.push(replacement_id.clone());
        self.schedule_report(
            &replacement_id,
            ExecutionType::Acknowledged,
            OrderStatus::Accepted,
            ZERO,
            None,
        )?;
        if let Some(market) = self.latest_markets.get(&replacement_symbol).cloned() {
            let decision = self.attempt(&replacement_id, &market, LiquidityRole::Taker, None)?;
            if decision.is_none() {
                let tif = self
                    .orders
                    .get(&replacement_id)
                    .map(|order| order.order.time_in_force);
                if tif == Some(TimeInForce::Ioc) {
                    self.schedule_expiry(&replacement_id, false)?;
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
    pub fn state(&self, order_id: &str) -> LiveResult<CryptoOrderState> {
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
            let decision =
                self.attempt(&order_id, &event, LiquidityRole::Maker, Some(available))?;
            match decision {
                Some(decision) => available -= decision.quantity,
                None => {
                    if tif == TimeInForce::Ioc {
                        self.schedule_expiry(&order_id, false)?;
                    }
                }
            }
        }
        Ok(())
    }

    /// Reconcile one fill's local fee against the venue schedule.
    ///
    /// A disagreement is recorded in the ledger and surfaced as an error so it
    /// is never silently tolerated.
    pub fn reconcile_fee(&mut self, fill: &Fill) -> LiveResult<Decimal18> {
        if !is_crypto_pair(fill.symbol.as_str()) {
            return Err(LiveError::Venue(
                "crypto fills require a BASE/QUOTE pair symbol".into(),
            ));
        }
        let Some(liquidity_text) = fill.liquidity.as_deref() else {
            return Err(LiveError::Venue(
                "crypto fills require maker or taker liquidity".into(),
            ));
        };
        let liquidity = LiquidityRole::parse(liquidity_text)?;
        let local_fee = self
            .local
            .calculate(fill)?
            .iter()
            .fold(ZERO, |sum, component| sum + component.amount);
        let venue_fee = self.schedule.charge(fill.quantity, fill.price, liquidity)?;
        self.ledger.push(CryptoFeeLedgerEntry {
            fill_id: fill.fill_id.clone(),
            order_id: fill.order_id.clone(),
            symbol: fill.symbol.clone(),
            side: fill.side,
            quantity: fill.quantity,
            price: fill.price,
            liquidity,
            local_fee,
            venue_fee,
        });
        if local_fee != venue_fee {
            return Err(LiveError::Venue(format!(
                "crypto fee mismatch for fill {}: local {} vs venue {}",
                fill.fill_id, local_fee, venue_fee
            )));
        }
        Ok(local_fee)
    }

    fn attempt(
        &mut self,
        order_id: &str,
        market: &MarketEvent,
        role: LiquidityRole,
        available: Option<Decimal18>,
    ) -> LiveResult<Option<FillDecision>> {
        let Some(remaining) = self
            .orders
            .get(order_id)
            .map(WorkingOrder::remaining_quantity)
        else {
            return Ok(None);
        };
        if !remaining.is_positive() {
            return Ok(None);
        }
        let order = self.orders.get(order_id).expect("present").order.clone();
        let Some(mut decision) =
            self.fill_model
                .decide(&order, market, Some(remaining), available, false)?
        else {
            return Ok(None);
        };
        decision.liquidity = role_name(role).to_string();
        if let Some(working) = self.orders.get_mut(order_id) {
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
        let order = self
            .orders
            .get(order_id)
            .ok_or_else(|| LiveError::Venue(format!("unknown order {order_id:?}")))?
            .order
            .clone();
        if decision.quantity
            > self
                .orders
                .get(order_id)
                .expect("present")
                .remaining_quantity()
        {
            return Err(LiveError::Venue(
                "fill decision exceeds the order remainder".into(),
            ));
        }
        let fill_id = self.next_id("fill");
        let provisional = Fill::new(
            &fill_id,
            order_id,
            order.symbol.clone(),
            order.side,
            decision.quantity,
            decision.price,
            ZERO,
            Some(decision.liquidity.clone()),
        )?;
        let fee = self.reconcile_fee(&provisional)?;
        let mut fill = provisional;
        fill.fee = fee;

        let status = {
            let working = self.orders.get_mut(order_id).expect("present");
            working.filled_quantity += decision.quantity;
            working.filled_notional += decision.quantity * decision.price;
            working.fees_paid += fee;
            working.last_decision = Some(decision.clone());
            working.status = if working.remaining_quantity().is_zero() {
                OrderStatus::Filled
            } else {
                OrderStatus::PartiallyFilled
            };
            working.response_pending = true;
            working.status
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
            decision.quantity,
            Some(decision.price),
        )?;
        self.outcomes.push(VenueOutcome::Fill(fill));
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

    fn next_id(&mut self, kind: &str) -> String {
        self.ordinal += 1;
        format!("{}-{}-{}", self.venue, kind, self.ordinal)
    }
}

fn snapshot(working: &WorkingOrder) -> CryptoOrderState {
    CryptoOrderState {
        order: working.order.clone(),
        status: working.status,
        filled_quantity: working.filled_quantity,
        remaining_quantity: working.remaining_quantity(),
        fees_paid: working.fees_paid,
        last_decision: working.last_decision.clone(),
    }
}

fn validate_crypto_order(order: &SingleLegOrder) -> LiveResult<()> {
    if !is_crypto_pair(order.symbol.as_str()) {
        return Err(LiveError::Venue(format!(
            "crypto orders must use BASE/QUOTE pairs, not {:?}",
            order.symbol.as_str()
        )));
    }
    if !matches!(order.order_type, OrderType::Market | OrderType::Limit) {
        return Err(LiveError::Venue(
            "crypto orders support market and limit types only".into(),
        ));
    }
    Ok(())
}
