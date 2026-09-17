//! Native Rust backtest engine core.
//!
//! This is the port of the deterministic execution half of the Python engine:
//! the event bus and its dispatch hash, the simulated venue with the
//! `simple_touch` fill model, fee assessment, the portfolio hub, the risk
//! pipeline, and the run artifact. Strategies, indicators, and filters stay
//! Python and are driven through [`StrategyRuntime`].

use std::cmp::Ordering;
use std::collections::{BTreeMap, BinaryHeap};

use num::PyDecimal;

use crate::backtest::config::BacktestConfig;
use crate::backtest::errors::{BacktestError, BacktestStatus};
use crate::backtest::fees::{
    aggregate_fees, fee_schedule_from_config, FeeAggregation, FeeAssessment, FeeFill, FeeSchedule,
    DEFAULT_FEE_PRECISION,
};
use crate::backtest::fixture::{DataPreflight, MarketEvent, ReplayFixture, SyntheticDataOptions};
use crate::backtest::manifest::{BacktestManifest, StrategyIdentity};
use crate::backtest::portfolio::{portfolio_json, PortfolioHub, PortfolioSnapshot};
use crate::canonical_json::JsonValue;
use crate::sha256::Sha256;

/// One reconstructed market event carrying Python-scale decimals.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EngineMarket {
    /// A top-of-book quote.
    Quote {
        /// Event identifier.
        event_id: String,
        /// Event time in epoch nanoseconds.
        timestamp_ns: i64,
        /// Global sequence.
        sequence: u64,
        /// Instrument symbol.
        symbol: String,
        /// Best bid.
        bid: PyDecimal,
        /// Best ask.
        ask: PyDecimal,
        /// Bid size.
        bid_quantity: PyDecimal,
        /// Ask size.
        ask_quantity: PyDecimal,
        /// Reporting venue.
        venue: String,
    },
    /// A trade print.
    Trade {
        /// Event identifier.
        event_id: String,
        /// Event time in epoch nanoseconds.
        timestamp_ns: i64,
        /// Global sequence.
        sequence: u64,
        /// Instrument symbol.
        symbol: String,
        /// Print price.
        price: PyDecimal,
        /// Print quantity.
        quantity: PyDecimal,
        /// Reporting venue.
        venue: String,
        /// Trade identifier.
        trade_id: String,
    },
}

impl EngineMarket {
    /// Event time in epoch nanoseconds.
    #[must_use]
    pub const fn timestamp_ns(&self) -> i64 {
        match self {
            Self::Quote { timestamp_ns, .. } | Self::Trade { timestamp_ns, .. } => *timestamp_ns,
        }
    }

    /// The instrument symbol.
    #[must_use]
    pub fn symbol(&self) -> &str {
        match self {
            Self::Quote { symbol, .. } | Self::Trade { symbol, .. } => symbol,
        }
    }

    /// The event identifier.
    #[must_use]
    pub fn event_id(&self) -> &str {
        match self {
            Self::Quote { event_id, .. } | Self::Trade { event_id, .. } => event_id,
        }
    }

    /// The source sequence.
    #[must_use]
    pub const fn sequence(&self) -> u64 {
        match self {
            Self::Quote { sequence, .. } | Self::Trade { sequence, .. } => *sequence,
        }
    }

    /// The exact price the engine records for this market event.
    #[must_use]
    pub fn mark_price(&self) -> PyDecimal {
        match self {
            Self::Quote { bid, ask, .. } => bid
                .checked_add(*ask)
                .and_then(|sum| sum.checked_div(PyDecimal::from_i128(2)))
                .unwrap_or(*bid),
            Self::Trade { price, .. } => *price,
        }
    }
}

/// One order intent produced by a Python strategy.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EngineOrder {
    /// Order identifier.
    pub order_id: String,
    /// Instrument symbol.
    pub symbol: String,
    /// `buy` or `sell`.
    pub side: String,
    /// Order quantity.
    pub quantity: PyDecimal,
    /// Order type (`market`, `limit`, `stop`, `stop_limit`).
    pub order_type: String,
    /// Time in force (`day`, `gtc`, `ioc`).
    pub time_in_force: String,
    /// Limit price, if any.
    pub limit_price: Option<PyDecimal>,
    /// Stop price, if any.
    pub stop_price: Option<PyDecimal>,
    /// Order timestamp in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Order sequence.
    pub sequence: u64,
    /// Order event identifier.
    pub event_id: String,
}

impl EngineOrder {
    /// The signed quantity against the current position.
    #[must_use]
    pub fn signed_quantity(&self) -> PyDecimal {
        if self.side == "buy" {
            self.quantity
        } else {
            self.quantity.negated()
        }
    }
}

/// One deterministic fill produced by the venue.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EngineFill {
    /// Fill identifier.
    pub event_id: String,
    /// Fill time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Fill sequence.
    pub sequence: u64,
    /// Fill identifier (same as `event_id`).
    pub fill_id: String,
    /// Order identifier.
    pub order_id: String,
    /// Instrument symbol.
    pub symbol: String,
    /// `buy` or `sell`.
    pub side: String,
    /// Fill quantity.
    pub quantity: PyDecimal,
    /// Fill price.
    pub price: PyDecimal,
    /// Fill fee.
    pub fee: PyDecimal,
    /// Liquidity role.
    pub liquidity: String,
}

/// One deterministic execution report.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EngineReport {
    /// Report event identifier.
    pub event_id: String,
    /// Report time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Report sequence.
    pub sequence: u64,
    /// Order identifier.
    pub order_id: String,
    /// Execution type (`acknowledged`, `fill`, ...).
    pub execution_type: String,
}

/// One bus event.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EngineEvent {
    /// A market event.
    Market(EngineMarket),
    /// An execution report.
    Report(EngineReport),
    /// A fill.
    Fill(EngineFill),
}

impl EngineEvent {
    /// Event time in epoch nanoseconds.
    #[must_use]
    pub const fn timestamp_ns(&self) -> i64 {
        match self {
            Self::Market(market) => market.timestamp_ns(),
            Self::Report(report) => report.timestamp_ns,
            Self::Fill(fill) => fill.timestamp_ns,
        }
    }

    /// Event sequence.
    #[must_use]
    pub const fn sequence(&self) -> u64 {
        match self {
            Self::Market(market) => market.sequence(),
            Self::Report(report) => report.sequence,
            Self::Fill(fill) => fill.sequence,
        }
    }

    /// Event identifier.
    #[must_use]
    pub fn event_id(&self) -> &str {
        match self {
            Self::Market(market) => market.event_id(),
            Self::Report(report) => &report.event_id,
            Self::Fill(fill) => &fill.event_id,
        }
    }

    /// Class priority used by the queue, mirroring `EVENT_CLASS_PRIORITY`.
    #[must_use]
    pub const fn class_priority(&self) -> u8 {
        match self {
            Self::Market(EngineMarket::Quote { .. }) => 0,
            Self::Market(EngineMarket::Trade { .. }) => 1,
            Self::Report(_) => 4,
            Self::Fill(_) => 5,
        }
    }

    /// Python `module.qualname` for the dispatch identity.
    #[must_use]
    pub const fn qualname(&self) -> &'static str {
        match self {
            Self::Market(EngineMarket::Quote { .. }) => "harness.core.types.events.QuoteEvent",
            Self::Market(EngineMarket::Trade { .. }) => "harness.core.types.events.TradeEvent",
            Self::Report(_) => "harness.core.types.accounting.ExecutionReport",
            Self::Fill(_) => "harness.core.types.accounting.Fill",
        }
    }

    /// Python `event_type.value` for the dispatch identity.
    #[must_use]
    pub const fn event_type(&self) -> &'static str {
        match self {
            Self::Market(EngineMarket::Quote { .. }) => "quote",
            Self::Market(EngineMarket::Trade { .. }) => "trade",
            Self::Report(_) => "execution_report",
            Self::Fill(_) => "fill",
        }
    }
}

/// One queue entry ordered by time, sequence, class, then feed order.
#[derive(Clone, Debug)]
struct QueuedEvent {
    event: EngineEvent,
    feed_ordinal: u64,
}

impl PartialEq for QueuedEvent {
    fn eq(&self, other: &Self) -> bool {
        self.key() == other.key()
    }
}
impl Eq for QueuedEvent {}
impl PartialOrd for QueuedEvent {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for QueuedEvent {
    fn cmp(&self, other: &Self) -> Ordering {
        // `BinaryHeap` is a max-heap; reverse so the smallest key pops first.
        other.key().cmp(&self.key())
    }
}

impl QueuedEvent {
    fn key(&self) -> (i64, u64, u8, u64) {
        (
            self.event.timestamp_ns(),
            self.event.sequence(),
            self.event.class_priority(),
            self.feed_ordinal,
        )
    }
}

/// The deterministic event bus and its Python-compatible dispatch digest.
#[derive(Debug, Default)]
pub struct EventBus {
    heap: BinaryHeap<QueuedEvent>,
    feed_ordinal: u64,
    hasher: Sha256,
    dispatched_count: u64,
}

impl EventBus {
    /// Create an empty bus.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Enqueue one event.
    pub fn schedule(&mut self, event: EngineEvent) {
        self.heap.push(QueuedEvent {
            event,
            feed_ordinal: self.feed_ordinal,
        });
        self.feed_ordinal += 1;
    }

    /// Pop and record the next event, or `None` when empty.
    pub fn dispatch_next(&mut self) -> Option<EngineEvent> {
        let queued = self.heap.pop()?;
        self.record_dispatch(&queued.event);
        Some(queued.event)
    }

    /// The stable SHA-256 digest of dispatched event identities.
    #[must_use]
    pub fn dispatch_hash(&self) -> String {
        self.hasher.clone().finalize_hex()
    }

    /// The number of events dispatched.
    #[must_use]
    pub const fn dispatched_count(&self) -> u64 {
        self.dispatched_count
    }

    fn record_dispatch(&mut self, event: &EngineEvent) {
        let identity = format!(
            "{}\u{0}{}\u{0}{}\u{0}{}\u{0}{}",
            event.qualname(),
            event.event_type(),
            crate::backtest::manifest::utc_timestamp(event.timestamp_ns(), false),
            event.sequence(),
            event.event_id(),
        );
        let bytes = identity.as_bytes();
        self.hasher.update(&(bytes.len() as u64).to_be_bytes());
        self.hasher.update(bytes);
        self.dispatched_count += 1;
    }
}

/// One accepted venue-side working order.
#[derive(Clone, Debug)]
struct WorkingOrder {
    order: EngineOrder,
    status: String,
    filled_quantity: PyDecimal,
    response_pending: bool,
}

impl WorkingOrder {
    fn remaining(&self) -> PyDecimal {
        self.order
            .quantity
            .checked_sub(self.filled_quantity)
            .unwrap_or(PyDecimal::ZERO)
    }

    fn is_active(&self) -> bool {
        !matches!(
            self.status.as_str(),
            "filled" | "canceled" | "rejected" | "expired" | "replaced"
        )
    }
}

/// Synchronous deterministic resting-order venue with `simple_touch` fills.
#[derive(Debug)]
pub struct SimulatedVenue {
    head_ns: Option<i64>,
    sequence: i64,
    ordinal: u64,
    orders: Vec<WorkingOrder>,
    latest_markets: BTreeMap<String, EngineMarket>,
}

impl Default for SimulatedVenue {
    fn default() -> Self {
        Self {
            head_ns: None,
            sequence: -1,
            ordinal: 0,
            orders: Vec::new(),
            latest_markets: BTreeMap::new(),
        }
    }
}

impl SimulatedVenue {
    /// Create an empty venue.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    fn observe(&mut self, timestamp_ns: i64, sequence: u64) {
        if self.head_ns.is_none_or(|head| timestamp_ns > head) {
            self.head_ns = Some(timestamp_ns);
        }
        if i64::try_from(sequence).unwrap_or(i64::MAX) > self.sequence {
            self.sequence = i64::try_from(sequence).unwrap_or(i64::MAX);
        }
    }

    fn next_sequence(&mut self) -> u64 {
        self.sequence += 1;
        u64::try_from(self.sequence).unwrap_or(0)
    }

    fn next_id(&mut self, kind: &str) -> String {
        self.ordinal += 1;
        format!("sim-{kind}-{}", self.ordinal)
    }

    const fn response_timestamp(&self, clock_ns: i64) -> i64 {
        match self.head_ns {
            Some(head) => {
                if clock_ns > head {
                    clock_ns
                } else {
                    head
                }
            }
            None => clock_ns,
        }
    }

    fn on_market(&mut self, market: &EngineMarket, clock_ns: i64, bus: &mut EventBus) {
        self.observe(market.timestamp_ns(), market.sequence());
        self.latest_markets
            .insert(market.symbol().to_string(), market.clone());
        for index in 0..self.orders.len() {
            if !self.orders[index].is_active() || self.orders[index].response_pending {
                continue;
            }
            if self.orders[index].order.symbol != market.symbol() {
                continue;
            }
            let side = self.orders[index].order.side.clone();
            let available = market_quantity(&side, market);
            let maybe = self.attempt(index, market, available, clock_ns, bus);
            if maybe.is_some() {
                // A single match per event is enough for the simple-touch model.
            }
        }
    }

    fn on_report(&mut self, report: &EngineReport) {
        self.observe(report.timestamp_ns, report.sequence);
        if report.execution_type == "acknowledged" {
            return;
        }
        if let Some(working) = self
            .orders
            .iter_mut()
            .find(|working| working.order.order_id == report.order_id)
        {
            working.response_pending = false;
        }
    }

    fn on_fill(&mut self, fill: &EngineFill) {
        self.observe(fill.timestamp_ns, fill.sequence);
    }

    fn submit(&mut self, order: EngineOrder, clock_ns: i64, bus: &mut EventBus) {
        self.observe(order.timestamp_ns, order.sequence);
        let working = WorkingOrder {
            order: order.clone(),
            status: "accepted".to_string(),
            filled_quantity: PyDecimal::ZERO,
            response_pending: false,
        };
        self.orders.push(working);
        self.schedule_report(&order, "acknowledged", "accepted", clock_ns, bus);
        if let Some(market) = self.latest_markets.get(&order.symbol).cloned() {
            let available = market_quantity(&order.side, &market);
            let index = self
                .orders
                .iter()
                .position(|working| working.order.order_id == order.order_id);
            if let Some(index) = index {
                let _ = self.attempt(index, &market, available, clock_ns, bus);
            }
        }
    }

    fn attempt(
        &mut self,
        index: usize,
        market: &EngineMarket,
        available: PyDecimal,
        clock_ns: i64,
        bus: &mut EventBus,
    ) -> Option<PyDecimal> {
        let (order, remaining) = {
            let working = &self.orders[index];
            (working.order.clone(), working.remaining())
        };
        if remaining.cmp_value(PyDecimal::ZERO) != Ordering::Greater {
            return None;
        }
        if available <= PyDecimal::ZERO {
            return None;
        }
        let decision = simple_touch_price(&order, market)?;
        let quantity = if remaining.cmp_value(available) == Ordering::Less {
            remaining
        } else {
            available
        };
        if quantity.cmp_value(PyDecimal::ZERO) != Ordering::Greater {
            return None;
        }
        self.schedule_fill(index, quantity, decision, clock_ns, bus);
        Some(quantity)
    }

    fn schedule_fill(
        &mut self,
        index: usize,
        quantity: PyDecimal,
        price: PyDecimal,
        clock_ns: i64,
        bus: &mut EventBus,
    ) {
        {
            let working = &mut self.orders[index];
            working.filled_quantity = working
                .filled_quantity
                .checked_add(quantity)
                .unwrap_or(working.filled_quantity);
            working.status = if working.remaining().is_zero() {
                "filled".to_string()
            } else {
                "partially_filled".to_string()
            };
            working.response_pending = true;
        }
        let order = self.orders[index].order.clone();
        self.schedule_report(&order, "fill", "filled", clock_ns, bus);
        let timestamp_ns = self.response_timestamp(clock_ns);
        let fill_id = self.next_id("fill");
        let sequence = self.next_sequence();
        bus.schedule(EngineEvent::Fill(EngineFill {
            event_id: fill_id.clone(),
            timestamp_ns,
            sequence,
            fill_id,
            order_id: order.order_id.clone(),
            symbol: order.symbol.clone(),
            side: order.side.clone(),
            quantity,
            price,
            fee: PyDecimal::ZERO,
            liquidity: "taker".to_string(),
        }));
    }

    fn schedule_report(
        &mut self,
        order: &EngineOrder,
        execution_type: &str,
        _status: &str,
        clock_ns: i64,
        bus: &mut EventBus,
    ) {
        let timestamp_ns = self.response_timestamp(clock_ns);
        let report_id = self.next_id("report");
        let sequence = self.next_sequence();
        bus.schedule(EngineEvent::Report(EngineReport {
            event_id: report_id,
            timestamp_ns,
            sequence,
            order_id: order.order_id.clone(),
            execution_type: execution_type.to_string(),
        }));
    }
}

fn market_quantity(side: &str, market: &EngineMarket) -> PyDecimal {
    match market {
        EngineMarket::Trade { quantity, .. } => *quantity,
        EngineMarket::Quote {
            bid_quantity,
            ask_quantity,
            ..
        } => {
            if side == "buy" {
                *ask_quantity
            } else {
                *bid_quantity
            }
        }
    }
}

fn simple_touch_price(order: &EngineOrder, market: &EngineMarket) -> Option<PyDecimal> {
    match market {
        EngineMarket::Trade { price, .. } => {
            if order.order_type == "market" || order.order_type == "stop" {
                Some(*price)
            } else {
                None
            }
        }
        EngineMarket::Quote { bid, ask, .. } => {
            if order.side == "buy" {
                let price = *ask;
                if order.order_type == "market" || order.order_type == "stop" {
                    return Some(price);
                }
                let limit = order.limit_price?;
                if price.cmp_value(limit) != Ordering::Greater {
                    Some(price)
                } else {
                    None
                }
            } else {
                let price = *bid;
                if order.order_type == "market" || order.order_type == "stop" {
                    return Some(price);
                }
                let limit = order.limit_price?;
                if price.cmp_value(limit) != Ordering::Less {
                    Some(price)
                } else {
                    None
                }
            }
        }
    }
}

/// One risk audit entry frozen for the artifact.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RiskAudit {
    /// Audit sequence.
    pub sequence: u64,
    /// Rule pass number.
    pub pass_number: u64,
    /// Rule identifier.
    pub rule_id: String,
    /// `allow`, `reduce`, or `reject`.
    pub action: String,
    /// Audit reason.
    pub reason: Option<String>,
    /// Whether the rule requested a halt.
    pub halt: bool,
}

/// One complete recorded risk decision.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RiskDecision {
    /// Decision action.
    pub action: String,
    /// Original order identifier.
    pub order_id: String,
    /// Strategy instance identifier.
    pub strategy_id: String,
    /// Decision reason.
    pub reason: Option<String>,
    /// Halt scope.
    pub halt_scope: Option<String>,
    /// Ordered audits.
    pub audits: Vec<RiskAudit>,
}

/// One configured risk rule resolved to a cap.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResolvedRiskRule {
    /// Rule identifier.
    pub rule_id: String,
    /// Rule kind.
    pub kind: RiskRuleKind,
    /// Numeric limit.
    pub limit: PyDecimal,
}

/// Supported built-in risk rule kinds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RiskRuleKind {
    /// Cap the gross notional of one candidate order (`MaxOrderNotional`).
    MaxOrderNotional,
    /// Cap the resulting notional of each changed position (`MaxPositionNotional`).
    MaxPositionNotional,
}

/// Resolve the configured risk rules into evaluated built-ins.
///
/// # Errors
///
/// Returns [`BacktestError::Configuration`] on an unsupported implementation or
/// missing limit.
pub fn resolve_risk_rules(
    configuration: &BacktestConfig,
) -> Result<Vec<ResolvedRiskRule>, BacktestError> {
    let mut rules = Vec::new();
    for rule in &configuration.risk_pipeline.rules {
        let class = rule
            .implementation
            .rsplit(':')
            .next()
            .unwrap_or(rule.implementation.as_str());
        let kind = match class {
            "MaxOrderNotional" => RiskRuleKind::MaxOrderNotional,
            "MaxPositionNotional" => RiskRuleKind::MaxPositionNotional,
            other => {
                return Err(BacktestError::Configuration(format!(
                    "unsupported risk rule implementation {other:?}"
                )));
            }
        };
        let limit = rule
            .parameters
            .get("limit")
            .and_then(parameter_decimal)
            .ok_or_else(|| {
                BacktestError::Configuration(format!(
                    "risk rule {:?} requires a 'limit' parameter",
                    rule.rule_id
                ))
            })?;
        rules.push(ResolvedRiskRule {
            rule_id: rule.rule_id.clone(),
            kind,
            limit,
        });
    }
    Ok(rules)
}

fn parameter_decimal(value: &JsonValue) -> Option<PyDecimal> {
    match value {
        JsonValue::String(text) => text.parse().ok(),
        JsonValue::Object(entries) => entries.get("$decimal").and_then(|inner| match inner {
            JsonValue::String(text) => text.parse().ok(),
            _ => None,
        }),
        JsonValue::Int(value) => Some(PyDecimal::from_i128(i128::from(*value))),
        _ => None,
    }
}

/// A strategy runtime driven by the engine, tagged with its instance.
pub struct StrategyInstance {
    /// Configured instance identifier.
    pub instance_id: String,
    /// The Python-backed runtime.
    pub runtime: Box<dyn StrategyRuntime>,
}

/// Startup facts handed to a strategy runtime.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StrategyStart {
    /// Instance identifier.
    pub instance_id: String,
    /// Shared account identifier.
    pub account_id: String,
    /// Strategy universe symbols.
    pub symbols: Vec<String>,
    /// Run seed.
    pub seed: u64,
    /// Initial book cash.
    pub initial_cash: PyDecimal,
    /// Run start in epoch nanoseconds.
    pub start_ns: i64,
}

/// The Python plugin seam the engine drives.
pub trait StrategyRuntime {
    /// Called once before the first market event.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError`] when the strategy cannot start.
    fn on_start(&mut self, start: &StrategyStart) -> Result<(), BacktestError>;
    /// Called for every market event; return the orders it submitted.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError`] when the strategy raises.
    fn on_market(&mut self, event: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError>;
    /// Called for every booked fill owned by this strategy.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError`] when the strategy raises.
    fn on_fill(&mut self, fill: &EngineFill) -> Result<(), BacktestError> {
        let _ = fill;
        Ok(())
    }
    /// Called once after the loop.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError`] when the strategy raises.
    fn on_stop(&mut self) -> Result<(), BacktestError> {
        Ok(())
    }
    /// Return the latest finite numeric run metrics published by this runtime.
    #[must_use]
    fn published_metrics(&self) -> BTreeMap<String, String> {
        BTreeMap::new()
    }
}

/// Explicit knobs for one native engine run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EngineOptions {
    /// String-exact fee precision.
    pub fee_precision: String,
    /// Fee asset class.
    pub asset_class: String,
}

impl Default for EngineOptions {
    fn default() -> Self {
        Self {
            fee_precision: DEFAULT_FEE_PRECISION.to_string(),
            asset_class: "equity".to_string(),
        }
    }
}

/// The complete outcome of one native engine run.
#[derive(Clone, Debug)]
pub struct BacktestOutcome {
    /// Reproducibility manifest.
    pub manifest: BacktestManifest,
    /// Terminal status.
    pub status: BacktestStatus,
    /// Abort reason, if any.
    pub reason: Option<String>,
    /// Preflight evidence.
    pub preflight: DataPreflight,
    /// Bus dispatch hash.
    pub dispatch_hash: String,
    /// Bus dispatched count.
    pub dispatched_count: u64,
    /// Market events consumed.
    pub market_event_count: u64,
    /// Approved orders.
    pub order_count: u64,
    /// Booked fills.
    pub fills: Vec<EngineFill>,
    /// Fee assessments.
    pub fee_assessments: Vec<FeeAssessment>,
    /// Fee aggregation.
    pub fee_totals: FeeAggregation,
    /// Portfolio snapshot.
    pub portfolio: PortfolioSnapshot,
    /// Risk decisions.
    pub risk_decisions: Vec<RiskDecision>,
    /// Halted strategies.
    pub halted_strategies: Vec<String>,
    /// Stopped strategies.
    pub stopped_strategies: Vec<String>,
    /// Teardown errors.
    pub teardown_errors: Vec<String>,
    /// Lookahead violations.
    pub lookahead_violations: Vec<String>,
    /// Custom finite numeric observations published by research or strategy code.
    pub custom_metrics: BTreeMap<String, String>,
}

impl BacktestOutcome {
    /// Whether the run replayed its complete fixture.
    #[must_use]
    pub fn completed(&self) -> bool {
        self.status == BacktestStatus::Completed
    }

    /// Return the canonical, JSON-ready run artifacts.
    #[must_use]
    pub fn artifacts(&self) -> JsonValue {
        let mut entries = vec![
            ("manifest", self.manifest.to_json()),
            ("status", JsonValue::string(self.status.value())),
            (
                "reason",
                match &self.reason {
                    Some(reason) => JsonValue::String(reason.clone()),
                    None => JsonValue::Null,
                },
            ),
            ("preflight", self.preflight.to_json()),
            (
                "dispatch_hash",
                JsonValue::String(self.dispatch_hash.clone()),
            ),
            (
                "counts",
                JsonValue::object([
                    ("dispatched", JsonValue::UInt(self.dispatched_count)),
                    ("market_events", JsonValue::UInt(self.market_event_count)),
                    ("orders", JsonValue::UInt(self.order_count)),
                    ("fills", JsonValue::UInt(self.fills.len() as u64)),
                    (
                        "risk_decisions",
                        JsonValue::UInt(self.risk_decisions.len() as u64),
                    ),
                ]),
            ),
            (
                "strategies",
                JsonValue::object([
                    ("halted", string_array(&self.halted_strategies)),
                    ("stopped", string_array(&self.stopped_strategies)),
                    ("teardown_errors", string_array(&self.teardown_errors)),
                    (
                        "lookahead_violations",
                        string_array(&self.lookahead_violations),
                    ),
                ]),
            ),
            (
                "fills",
                JsonValue::Array(self.fills.iter().map(fill_json).collect()),
            ),
            ("fees", fees_json(&self.fee_assessments, &self.fee_totals)),
            ("portfolio", portfolio_json(&self.portfolio)),
            (
                "risk",
                JsonValue::Array(self.risk_decisions.iter().map(risk_json).collect()),
            ),
        ];
        if !self.custom_metrics.is_empty() {
            entries.push((
                "custom_metrics",
                JsonValue::object(
                    self.custom_metrics
                        .iter()
                        .map(|(name, value)| (name.clone(), JsonValue::String(value.clone()))),
                ),
            ));
        }
        JsonValue::object(entries)
    }

    /// Return the stable byte-for-byte artifact document for this run.
    #[must_use]
    pub fn artifact_json(&self) -> String {
        self.artifacts().to_canonical_string()
    }

    /// Return the SHA-256 digest of the complete run artifact.
    #[must_use]
    pub fn artifact_digest(&self) -> String {
        crate::sha256::digest_hex(self.artifact_json().as_bytes())
    }

    /// Return the SHA-256 digest of the run outcome without provenance.
    #[must_use]
    pub fn outcome_digest(&self) -> String {
        let artifacts = self.artifacts();
        let JsonValue::Object(mut entries) = artifacts else {
            unreachable!("artifacts is an object")
        };
        entries.remove("manifest");
        entries.remove("preflight");
        crate::sha256::digest_hex(JsonValue::Object(entries).to_canonical_string().as_bytes())
    }
}

fn string_array(values: &[String]) -> JsonValue {
    JsonValue::Array(
        values
            .iter()
            .map(|value| JsonValue::String(value.clone()))
            .collect(),
    )
}

fn fill_json(fill: &EngineFill) -> JsonValue {
    JsonValue::object([
        ("event_id", JsonValue::String(fill.event_id.clone())),
        (
            "timestamp",
            JsonValue::String(crate::backtest::manifest::utc_timestamp(
                fill.timestamp_ns,
                false,
            )),
        ),
        ("sequence", JsonValue::UInt(fill.sequence)),
        ("fill_id", JsonValue::String(fill.fill_id.clone())),
        ("order_id", JsonValue::String(fill.order_id.clone())),
        ("symbol", JsonValue::String(fill.symbol.clone())),
        ("side", JsonValue::String(fill.side.clone())),
        ("quantity", JsonValue::String(fill.quantity.to_string())),
        ("price", JsonValue::String(fill.price.to_string())),
        ("fee", JsonValue::String(fill.fee.to_string())),
        (
            "liquidity",
            match fill.liquidity.as_str() {
                "" => JsonValue::Null,
                value => JsonValue::String(value.to_string()),
            },
        ),
    ])
}

fn fees_json(assessments: &[FeeAssessment], totals: &FeeAggregation) -> JsonValue {
    JsonValue::object([
        (
            "assessments",
            JsonValue::Array(
                assessments
                    .iter()
                    .map(|assessment| {
                        JsonValue::object([
                            ("fill_id", JsonValue::String(assessment.fill_id.clone())),
                            (
                                "schedule_id",
                                JsonValue::String(assessment.schedule_id.clone()),
                            ),
                            ("currency", JsonValue::String(assessment.currency.clone())),
                            ("run_id", JsonValue::String(assessment.run_id.clone())),
                            (
                                "strategy_id",
                                JsonValue::String(assessment.strategy_id.clone()),
                            ),
                            (
                                "asset_class",
                                JsonValue::String(assessment.asset_class.clone()),
                            ),
                            ("total", JsonValue::String(assessment.total().to_string())),
                            (
                                "components",
                                JsonValue::Array(
                                    assessment
                                        .components
                                        .iter()
                                        .map(|component| {
                                            JsonValue::object([
                                                (
                                                    "fee_id",
                                                    JsonValue::String(component.fee_id.clone()),
                                                ),
                                                (
                                                    "amount",
                                                    JsonValue::String(component.amount.to_string()),
                                                ),
                                            ])
                                        })
                                        .collect(),
                                ),
                            ),
                        ])
                    })
                    .collect(),
            ),
        ),
        (
            "by_run",
            JsonValue::Array(totals.by_run.iter().map(aggregate_json).collect()),
        ),
        (
            "by_strategy",
            JsonValue::Array(totals.by_strategy.iter().map(aggregate_json).collect()),
        ),
        (
            "by_asset_class",
            JsonValue::Array(totals.by_asset_class.iter().map(aggregate_json).collect()),
        ),
    ])
}

fn aggregate_json(aggregate: &crate::backtest::fees::FeeAggregate) -> JsonValue {
    JsonValue::object([
        ("key", JsonValue::String(aggregate.key.clone())),
        ("currency", JsonValue::String(aggregate.currency.clone())),
        ("amount", JsonValue::String(aggregate.amount.to_string())),
    ])
}

fn risk_json(decision: &RiskDecision) -> JsonValue {
    JsonValue::object([
        ("action", JsonValue::String(decision.action.clone())),
        ("order_id", JsonValue::String(decision.order_id.clone())),
        (
            "strategy_id",
            JsonValue::String(decision.strategy_id.clone()),
        ),
        (
            "reason",
            match &decision.reason {
                Some(reason) => JsonValue::String(reason.clone()),
                None => JsonValue::Null,
            },
        ),
        (
            "halt_scope",
            match &decision.halt_scope {
                Some(scope) => JsonValue::String(scope.clone()),
                None => JsonValue::Null,
            },
        ),
        (
            "audits",
            JsonValue::Array(
                decision
                    .audits
                    .iter()
                    .map(|audit| {
                        JsonValue::object([
                            ("sequence", JsonValue::UInt(audit.sequence)),
                            ("pass_number", JsonValue::UInt(audit.pass_number)),
                            ("rule_id", JsonValue::String(audit.rule_id.clone())),
                            ("action", JsonValue::String(audit.action.clone())),
                            (
                                "reason",
                                match &audit.reason {
                                    Some(reason) => JsonValue::String(reason.clone()),
                                    None => JsonValue::Null,
                                },
                            ),
                            ("halt", JsonValue::Bool(audit.halt)),
                        ])
                    })
                    .collect(),
            ),
        ),
    ])
}

/// Convert one fixture event to a Python-scale market event.
#[must_use]
pub fn market_event(event: &MarketEvent, options: &SyntheticDataOptions) -> EngineMarket {
    let (quote_scale, trade_scale) = crate::backtest::fixture::synthetic_scales(options);
    match event {
        MarketEvent::Quote {
            event_id,
            timestamp_ns,
            sequence,
            symbol,
            bid,
            ask,
            bid_quantity,
            ask_quantity,
            venue,
        } => EngineMarket::Quote {
            event_id: event_id.clone(),
            timestamp_ns: *timestamp_ns,
            sequence: *sequence,
            symbol: symbol.clone(),
            bid: py_from(*bid, quote_scale),
            ask: py_from(*ask, quote_scale),
            bid_quantity: py_from(*bid_quantity, options.quantity_scale),
            ask_quantity: py_from(*ask_quantity, options.quantity_scale),
            venue: venue.clone(),
        },
        MarketEvent::Trade {
            event_id,
            timestamp_ns,
            sequence,
            symbol,
            price,
            quantity,
            venue,
            trade_id,
        } => EngineMarket::Trade {
            event_id: event_id.clone(),
            timestamp_ns: *timestamp_ns,
            sequence: *sequence,
            symbol: symbol.clone(),
            price: py_from(*price, trade_scale),
            quantity: py_from(*quantity, options.quantity_scale),
            venue: venue.clone(),
            trade_id: trade_id.clone(),
        },
    }
}

fn py_from(value: num::Decimal18, scale: u32) -> PyDecimal {
    crate::backtest::fixture::python_decimal(value, scale)
        .parse()
        .unwrap_or(PyDecimal::ZERO)
}

/// Evaluate one configured rule against a candidate order.
fn evaluate_rule(
    rule: &ResolvedRiskRule,
    order: &EngineOrder,
    prices: &BTreeMap<String, PyDecimal>,
    current_quantity: PyDecimal,
) -> (String, Option<String>) {
    match rule.kind {
        RiskRuleKind::MaxOrderNotional => {
            let Some(price) = order_price(order, prices) else {
                return (
                    "reject".to_string(),
                    Some("max_order_notional missing price".to_string()),
                );
            };
            let notional = order.quantity.checked_mul(price).unwrap_or(PyDecimal::ZERO);
            if notional.cmp_value(rule.limit) != Ordering::Greater {
                ("allow".to_string(), None)
            } else {
                (
                    "reject".to_string(),
                    Some("max_order_notional has no permitted quantity".to_string()),
                )
            }
        }
        RiskRuleKind::MaxPositionNotional => {
            let Some(price) = order_price(order, prices) else {
                return (
                    "reject".to_string(),
                    Some("max_position_notional missing price".to_string()),
                );
            };
            let projected = current_quantity
                .checked_add(order.signed_quantity())
                .map(|value| value.abs())
                .and_then(|value| value.checked_mul(price))
                .unwrap_or(PyDecimal::ZERO);
            if projected.cmp_value(rule.limit) != Ordering::Greater {
                ("allow".to_string(), None)
            } else {
                (
                    "reject".to_string(),
                    Some("max_position_notional has no permitted quantity".to_string()),
                )
            }
        }
    }
}

fn order_price(order: &EngineOrder, prices: &BTreeMap<String, PyDecimal>) -> Option<PyDecimal> {
    if let Some(limit) = order.limit_price {
        return Some(limit);
    }
    if let Some(stop) = order.stop_price {
        return Some(stop);
    }
    prices.get(&order.symbol).copied()
}

/// Run one native engine over a fixture and a set of strategy runtimes.
///
/// # Errors
///
/// Returns [`BacktestError`] when the fee schedule or risk rules are invalid,
/// a strategy fails, or replay verification fails.
#[allow(clippy::too_many_arguments)]
pub fn run_native_engine(
    configuration: &BacktestConfig,
    fixture: &ReplayFixture,
    preflight: &DataPreflight,
    manifest: BacktestManifest,
    options: &EngineOptions,
    strategies: Vec<StrategyInstance>,
    synthetic_options: &SyntheticDataOptions,
) -> Result<BacktestOutcome, BacktestError> {
    let schedule = fee_schedule_from_config(
        &configuration.fees,
        &options.fee_precision,
        "harness-config",
    )?;
    let rules = resolve_risk_rules(configuration)?;
    let mut hub = PortfolioHub::new(&configuration.run_id);
    for instance in &strategies {
        let capital_fraction = capital_fraction(configuration, &instance.instance_id);
        let initial_cash = configuration
            .initial_cash_decimal
            .checked_mul(capital_fraction)
            .unwrap_or(configuration.initial_cash_decimal);
        hub.create_book(&instance.instance_id, initial_cash);
    }

    let mut strategies = strategies;
    for instance in &mut strategies {
        let capital_fraction = capital_fraction(configuration, &instance.instance_id);
        let initial_cash = configuration
            .initial_cash_decimal
            .checked_mul(capital_fraction)
            .unwrap_or(configuration.initial_cash_decimal);
        let start = StrategyStart {
            instance_id: instance.instance_id.clone(),
            account_id: configuration.run_id.clone(),
            symbols: configuration.symbols.clone(),
            seed: configuration.seed,
            initial_cash,
            start_ns: configuration.start_ns,
        };
        instance.runtime.on_start(&start)?;
    }

    let mut bus = EventBus::new();
    let mut venue = SimulatedVenue::new();
    let mut prices: BTreeMap<String, PyDecimal> = BTreeMap::new();
    let mut order_owners: BTreeMap<String, String> = BTreeMap::new();
    let mut order_counts: BTreeMap<String, u64> = BTreeMap::new();
    let mut fills: Vec<EngineFill> = Vec::new();
    let mut assessments: Vec<FeeAssessment> = Vec::new();
    let mut risk_decisions: Vec<RiskDecision> = Vec::new();
    let mut market_event_count = 0u64;

    for event in &fixture.events {
        market_event_count += 1;
        let market = market_event(event, synthetic_options);
        bus.schedule(EngineEvent::Market(market));
        while let Some(dispatched) = bus.dispatch_next() {
            dispatch_event(
                &dispatched,
                configuration,
                options,
                &schedule,
                &rules,
                &mut bus,
                &mut venue,
                &mut hub,
                &mut prices,
                &mut order_owners,
                &mut order_counts,
                &mut fills,
                &mut assessments,
                &mut risk_decisions,
                &mut strategies,
            )?;
        }
    }

    let mut stopped = Vec::new();
    let mut teardown_errors = Vec::new();
    let mut custom_metrics = BTreeMap::new();
    let mut metric_owners = BTreeMap::new();
    for instance in &mut strategies {
        if let Err(error) = instance.runtime.on_stop() {
            teardown_errors.push(error.to_string());
        }
        for (name, value) in instance.runtime.published_metrics() {
            if let Some(owner) = metric_owners.get(&name) {
                teardown_errors.push(format!(
                    "custom metric {name:?} was published by both {owner:?} and {:?}",
                    instance.instance_id
                ));
                custom_metrics.remove(&name);
                continue;
            }
            metric_owners.insert(name.clone(), instance.instance_id.clone());
            custom_metrics.insert(name, value);
        }
        stopped.push(instance.instance_id.clone());
    }

    let order_count: u64 = order_counts.values().sum();
    verify_replay(fixture, preflight, market_event_count)?;
    let fee_totals = aggregate_fees(&assessments);
    Ok(BacktestOutcome {
        manifest,
        status: BacktestStatus::Completed,
        reason: None,
        preflight: preflight.clone(),
        dispatch_hash: bus.dispatch_hash(),
        dispatched_count: bus.dispatched_count(),
        market_event_count,
        order_count,
        fills,
        fee_assessments: assessments,
        fee_totals,
        portfolio: hub.snapshot(),
        risk_decisions,
        halted_strategies: Vec::new(),
        stopped_strategies: stopped,
        teardown_errors,
        lookahead_violations: Vec::new(),
        custom_metrics,
    })
}

#[allow(clippy::too_many_arguments)]
fn dispatch_event(
    event: &EngineEvent,
    configuration: &BacktestConfig,
    options: &EngineOptions,
    schedule: &FeeSchedule,
    rules: &[ResolvedRiskRule],
    bus: &mut EventBus,
    venue: &mut SimulatedVenue,
    hub: &mut PortfolioHub,
    prices: &mut BTreeMap<String, PyDecimal>,
    order_owners: &mut BTreeMap<String, String>,
    order_counts: &mut BTreeMap<String, u64>,
    fills: &mut Vec<EngineFill>,
    assessments: &mut Vec<FeeAssessment>,
    risk_decisions: &mut Vec<RiskDecision>,
    strategies: &mut [StrategyInstance],
) -> Result<(), BacktestError> {
    match event {
        EngineEvent::Market(market) => {
            let clock_ns = market.timestamp_ns();
            venue.on_market(market, clock_ns, bus);
            let mark = market.mark_price();
            prices.insert(market.symbol().to_string(), mark);
            hub.mark(market.symbol(), mark);
            for instance in strategies.iter_mut() {
                let orders = instance.runtime.on_market(market)?;
                for order in orders {
                    process_order(
                        &order,
                        &instance.instance_id,
                        configuration,
                        options,
                        schedule,
                        rules,
                        clock_ns,
                        bus,
                        venue,
                        hub,
                        prices,
                        order_owners,
                        order_counts,
                        risk_decisions,
                    );
                }
            }
        }
        EngineEvent::Report(report) => {
            venue.on_report(report);
        }
        EngineEvent::Fill(fill) => {
            venue.on_fill(fill);
            let owner = order_owners.get(&fill.order_id).cloned();
            let strategy_id = owner.clone().unwrap_or_else(|| "unattributed".to_string());
            let fee_fill = FeeFill {
                fill_id: &fill.fill_id,
                side: &fill.side,
                quantity: fill.quantity,
                price: fill.price,
                liquidity: Some(&fill.liquidity),
            };
            let assessment = schedule.calculate(
                &fee_fill,
                &configuration.run_id,
                &strategy_id,
                &options.asset_class,
            );
            let booked = EngineFill {
                fee: assessment.total(),
                ..fill.clone()
            };
            assessments.push(assessment);
            if let Some(owner) = &owner {
                hub.apply_fill(
                    owner,
                    &crate::backtest::portfolio::BookFill {
                        fill_id: booked.fill_id.clone(),
                        side: booked.side.clone(),
                        quantity: booked.quantity,
                        price: booked.price,
                        fee: booked.fee,
                        symbol: booked.symbol.clone(),
                    },
                );
            }
            fills.push(booked.clone());
            if let Some(owner) = &owner {
                if let Some(instance) = strategies
                    .iter_mut()
                    .find(|instance| &instance.instance_id == owner)
                {
                    instance.runtime.on_fill(&booked)?;
                }
            }
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn process_order(
    order: &EngineOrder,
    instance_id: &str,
    _configuration: &BacktestConfig,
    _options: &EngineOptions,
    _schedule: &FeeSchedule,
    rules: &[ResolvedRiskRule],
    clock_ns: i64,
    bus: &mut EventBus,
    venue: &mut SimulatedVenue,
    hub: &PortfolioHub,
    prices: &BTreeMap<String, PyDecimal>,
    order_owners: &mut BTreeMap<String, String>,
    order_counts: &mut BTreeMap<String, u64>,
    risk_decisions: &mut Vec<RiskDecision>,
) {
    let snapshot = hub.snapshot();
    let current_quantity = snapshot
        .positions
        .iter()
        .find(|position| position.symbol == order.symbol)
        .map_or(PyDecimal::ZERO, |position| position.quantity);
    let mut audits = Vec::new();
    let mut reason = None;
    let mut action = "allow".to_string();
    for rule in rules {
        let (rule_action, rule_reason) = evaluate_rule(rule, order, prices, current_quantity);
        audits.push(RiskAudit {
            sequence: audits.len() as u64,
            pass_number: 1,
            rule_id: rule.rule_id.clone(),
            action: rule_action.clone(),
            reason: rule_reason.clone(),
            halt: false,
        });
        if rule_action == "reject" {
            action = "reject".to_string();
            reason = rule_reason;
            break;
        }
    }
    risk_decisions.push(RiskDecision {
        action: action.clone(),
        order_id: order.order_id.clone(),
        strategy_id: instance_id.to_string(),
        reason: reason.clone(),
        halt_scope: None,
        audits,
    });
    if action == "reject" {
        return;
    }
    *order_counts.entry(instance_id.to_string()).or_insert(0) += 1;
    order_owners.insert(order.order_id.clone(), instance_id.to_string());
    venue.submit(order.clone(), clock_ns, bus);
}

fn capital_fraction(configuration: &BacktestConfig, instance_id: &str) -> PyDecimal {
    configuration
        .strategy_instances
        .iter()
        .find(|instance| instance.instance_id == instance_id)
        .and_then(|instance| instance.capital_fraction.parse::<PyDecimal>().ok())
        .unwrap_or(PyDecimal::ONE)
}

fn verify_replay(
    fixture: &ReplayFixture,
    preflight: &DataPreflight,
    market_event_count: u64,
) -> Result<(), BacktestError> {
    if market_event_count != preflight.event_count as u64 {
        return Err(BacktestError::DataPreflight(
            "replayed event count does not match the preflighted fixture".to_string(),
        ));
    }
    if fixture.fingerprints != preflight.fingerprints {
        return Err(BacktestError::DataPreflight(
            "replayed data fingerprint does not match the preflighted fixture".to_string(),
        ));
    }
    Ok(())
}

/// Build the reproducibility manifest for one native engine run.
///
/// # Errors
///
/// Returns [`BacktestError`] when the risk configuration is invalid.
pub fn build_default_manifest(
    configuration: &BacktestConfig,
    fixture: &ReplayFixture,
    preflight: &DataPreflight,
) -> Result<BacktestManifest, BacktestError> {
    let config_json = configuration.canonical_json();
    let JsonValue::Object(mut entries) = config_json else {
        unreachable!("configuration canonical json is an object");
    };
    let fees = entries.remove("fees").unwrap_or(JsonValue::Null);
    let risk = entries.remove("risk_pipeline").unwrap_or(JsonValue::Null);
    let rules = resolve_risk_rules(configuration)?;
    let rule_ids: Vec<JsonValue> = rules
        .iter()
        .map(|rule| JsonValue::String(rule.rule_id.clone()))
        .collect();
    let fill_identity = JsonValue::object([("model", JsonValue::string("simple_touch"))]);
    let fee_identity = JsonValue::object([
        ("fees", fees),
        ("precision", JsonValue::string("0.0001")),
        (
            "schedule_id",
            JsonValue::String("harness-config".to_string()),
        ),
    ]);
    let risk_identity =
        JsonValue::object([("pipeline", risk), ("rules", JsonValue::Array(rule_ids))]);
    crate::backtest::manifest::build_manifest(
        configuration,
        fixture,
        preflight,
        "simple_touch",
        &fill_identity,
        "harness-config",
        &fee_identity,
        &risk_identity,
        strategy_identities(configuration),
    )
}

/// Build the manifest strategy identities from configuration.
#[must_use]
pub fn strategy_identities(configuration: &BacktestConfig) -> Vec<StrategyIdentity> {
    configuration
        .strategy_instances
        .iter()
        .map(|instance| StrategyIdentity {
            instance_id: instance.instance_id.clone(),
            implementation: instance.implementation.clone(),
            bundle_hash: instance.bundle_hash.clone(),
            parameter_hash: crate::backtest::manifest::configuration_hash(&JsonValue::Object(
                instance.parameters.clone(),
            )),
            symbols: instance.symbols.clone(),
            capital_fraction: instance.capital_fraction.clone(),
        })
        .collect()
}
