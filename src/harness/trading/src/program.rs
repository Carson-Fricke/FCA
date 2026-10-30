//! Owns program observation/order contracts consumed by replay and authored adapters.
//! No acquisition, configuration parsing, storage or job orchestration.
use crate::replay_errors::BacktestError;
use num::Decimal;
use serde::Deserialize;
use serde::Serialize;
use std::collections::BTreeMap;

/// Exact consumer inputs, including the union of program-owned dependencies.
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MarketSubscription {
    /// `quote`, `trade`, or `bars`.
    pub data_type: String,
    /// Explicit instrument symbols.
    pub symbols: Vec<String>,
    /// Required for bars, absent for ticks.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub interval: Option<String>,
}

/// One reconstructed OHLCV bar carrying Python-scale decimals.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EngineBar {
    /// Event identifier.
    pub event_id: String,
    /// Event time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Global sequence.
    pub sequence: u64,
    /// Instrument symbol.
    pub symbol: String,
    /// Interval open.
    pub open: Decimal,
    /// Interval high.
    pub high: Decimal,
    /// Interval low.
    pub low: Decimal,
    /// Interval close.
    pub close: Decimal,
    /// Interval volume.
    pub volume: Decimal,
}

/// Atomic bar observation. Availability assumes interval completion, not provider latency.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EngineBarFrame {
    /// Canonical declared minute/hour/session interval identity.
    pub interval: String,
    /// Inclusive interval start in epoch nanoseconds.
    pub interval_start_ns: i64,
    /// Exclusive interval end in epoch nanoseconds.
    pub interval_end_ns: i64,
    /// Earliest dispatch time, currently the interval end.
    pub available_at_ns: i64,
    /// Observed bars in deterministic symbol order; never carried or filled.
    pub bars_by_symbol: BTreeMap<String, EngineBar>,
    /// Configured symbols absent from this observation, in sorted order.
    pub missing_symbols: Vec<String>,
}

/// One reconstructed market event carrying Python-scale decimals.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EngineMarket {
    /// A borrowed observation backed by an immutable historical tick block.
    Block(crate::market::BlockEvent),
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
        bid: Decimal,
        /// Best ask.
        ask: Decimal,
        /// Bid size.
        bid_quantity: Decimal,
        /// Ask size.
        ask_quantity: Decimal,
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
        price: Decimal,
        /// Print quantity.
        quantity: Decimal,
        /// Reporting venue.
        venue: String,
        /// Trade identifier.
        trade_id: String,
    },
    /// One OHLCV bar.
    ///
    /// Bars are dispatched only to runtimes that consume bars; the simulated
    /// venue and the last-trade model ignore them by construction.
    Bar(EngineBar),
}

impl EngineMarket {
    #[inline]
    pub fn view(&self) -> crate::market::MarketView<'_> {
        use crate::market::MarketView;
        match self {
            Self::Block(event) => event.view(),
            Self::Quote {
                event_id,
                timestamp_ns,
                sequence,
                symbol,
                bid,
                ask,
                bid_quantity,
                ask_quantity,
                venue,
            } => MarketView::Quote {
                event_id,
                timestamp_ns: *timestamp_ns,
                sequence: *sequence,
                symbol,
                bid: *bid,
                ask: *ask,
                bid_quantity: *bid_quantity,
                ask_quantity: *ask_quantity,
                venue,
            },
            Self::Trade {
                event_id,
                timestamp_ns,
                sequence,
                symbol,
                price,
                quantity,
                venue,
                trade_id,
            } => MarketView::Trade {
                event_id,
                timestamp_ns: *timestamp_ns,
                sequence: *sequence,
                symbol,
                price: *price,
                quantity: *quantity,
                venue,
                trade_id,
            },
            Self::Bar(bar) => MarketView::Bar(bar),
        }
    }
    #[inline]
    pub fn data_type(&self) -> &'static str {
        match self {
            Self::Quote { .. } => "quote",
            Self::Trade { .. } => "trade",
            Self::Bar(_) => "bars",
            Self::Block(event) => match event.kind() {
                crate::market::TickKind::Quote => "quote",
                crate::market::TickKind::Trade => "trade",
            },
        }
    }
    /// Event time in epoch nanoseconds.
    #[must_use]
    #[inline]
    pub fn timestamp_ns(&self) -> i64 {
        match self {
            Self::Block(event) => event.row().timestamp_ns,
            Self::Quote { timestamp_ns, .. }
            | Self::Trade { timestamp_ns, .. }
            | Self::Bar(EngineBar { timestamp_ns, .. }) => *timestamp_ns,
        }
    }

    /// The instrument symbol.
    #[must_use]
    #[inline]
    pub fn symbol(&self) -> &str {
        match self {
            Self::Block(event) => event.symbol(),
            Self::Quote { symbol, .. }
            | Self::Trade { symbol, .. }
            | Self::Bar(EngineBar { symbol, .. }) => symbol,
        }
    }

    /// The event identifier.
    #[must_use]
    #[inline]
    pub fn event_id(&self) -> &str {
        match self {
            Self::Block(event) => event.event_id(),
            Self::Quote { event_id, .. }
            | Self::Trade { event_id, .. }
            | Self::Bar(EngineBar { event_id, .. }) => event_id,
        }
    }

    /// The source sequence.
    #[must_use]
    #[inline]
    pub fn sequence(&self) -> u64 {
        match self {
            Self::Block(event) => event.row().sequence,
            Self::Quote { sequence, .. }
            | Self::Trade { sequence, .. }
            | Self::Bar(EngineBar { sequence, .. }) => *sequence,
        }
    }

    /// The exact price the engine records for this market event.
    #[must_use]
    #[inline]
    pub fn mark_price(&self) -> Decimal {
        match self.view() {
            crate::market::MarketView::Quote { bid, ask, .. } => bid
                .checked_add(ask)
                .and_then(|sum| sum.checked_div(Decimal::from_i128(2)))
                .unwrap_or(bid),
            crate::market::MarketView::Trade { price, .. } => price,
            crate::market::MarketView::Bar(EngineBar { close, .. }) => *close,
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
    pub quantity: Decimal,
    /// Order type (`market`, `limit`, `stop`, `stop_limit`).
    pub order_type: String,
    /// Time in force (`day`, `gtc`, `ioc`).
    pub time_in_force: String,
    /// Limit price, if any.
    pub limit_price: Option<Decimal>,
    /// Stop price, if any.
    pub stop_price: Option<Decimal>,
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
    pub fn signed_quantity(&self) -> Decimal {
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
    pub quantity: Decimal,
    /// Fill price.
    pub price: Decimal,
    /// Fill fee.
    pub fee: Decimal,
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
    pub initial_cash: Decimal,
    /// Run start in epoch nanoseconds.
    pub start_ns: i64,
}

/// One strategy's booked execution state at the current causal boundary.
/// The engine supplies this view; authored programs never reconstruct it from fills.
#[derive(Clone, Debug)]
pub struct ExecutionSnapshot {
    pub timestamp_ns: i64,
    pub initial_cash: Decimal,
    pub cash: Decimal,
    pub fees: Decimal,
    pub equity: Option<Decimal>,
    pub realized_pnl: Decimal,
    pub unrealized_pnl: Option<Decimal>,
    pub gross_exposure: Option<Decimal>,
    pub positions: Vec<ExecutionPosition>,
    pub orders: std::sync::Arc<Vec<ExecutionOrder>>,
    /// Canonical FIFO slices, using the same kernel as metrics.json.
    pub closed_trades: std::sync::Arc<Vec<crate::fills::trades::ClosedTrade>>,
}

/// Average-cost position accounting, including immediately recognized fill fees.
#[derive(Clone, Debug)]
pub struct ExecutionPosition {
    pub symbol: String,
    pub quantity: Decimal,
    pub average_price: Decimal,
    pub realized_pnl: Decimal,
    pub unrealized_pnl: Option<Decimal>,
    pub market_value: Option<Decimal>,
}

/// An owned order's actual lifecycle. Filled quantity includes booked fills only.
#[derive(Clone, Debug)]
pub struct ExecutionOrder {
    pub order_id: String,
    pub symbol: String,
    pub side: String,
    pub quantity: Decimal,
    pub filled_quantity: Decimal,
    pub remaining_quantity: Decimal,
    pub status: String,
    pub reason: Option<String>,
}

impl ExecutionSnapshot {
    /// Exact strings are the SDK wire representation; no decimal crosses as a float.
    pub fn account_json(&self) -> serde_json::Value {
        let optional = |value: Option<Decimal>| value.map(|v| v.to_string());
        serde_json::json!({
            "timestamp_ns": self.timestamp_ns,
            "initial_cash": self.initial_cash.to_string(), "cash": self.cash.to_string(),
            "fees": self.fees.to_string(), "equity": optional(self.equity),
            "realized_pnl": self.realized_pnl.to_string(),
            "unrealized_pnl": optional(self.unrealized_pnl),
            "gross_exposure": optional(self.gross_exposure),
            "positions": self.positions.iter().map(|p| serde_json::json!({
                "symbol": p.symbol, "quantity": p.quantity.to_string(),
                "average_price": p.average_price.to_string(),
                "realized_pnl": p.realized_pnl.to_string(),
                "unrealized_pnl": optional(p.unrealized_pnl),
                "market_value": optional(p.market_value),
            })).collect::<Vec<_>>(),
        })
    }

    /// Order history is independent of the frequently changing account marks.
    pub fn orders_json(&self) -> serde_json::Value {
        serde_json::Value::Array(self.orders.iter().map(|o| serde_json::json!({
                "order_id": o.order_id, "symbol": o.symbol, "side": o.side,
                "quantity": o.quantity.to_string(), "filled_quantity": o.filled_quantity.to_string(),
                "remaining_quantity": o.remaining_quantity.to_string(),
                "status": o.status, "reason": o.reason,
            })).collect())
    }

    /// Closed trades are projected only when the immutable history changes.
    pub fn closed_trades_json(&self) -> serde_json::Value {
        serde_json::Value::Array(self.closed_trades.iter().map(|t| serde_json::json!({
                "trade_id": t.trade_id, "symbol": t.symbol, "side": t.side,
                "quantity": t.quantity.to_string(), "entry_price": t.entry_price.to_string(),
                "exit_price": t.exit_price.to_string(), "gross_pnl": t.gross_pnl.to_string(),
                "fees": t.fees.to_string(), "net_pnl": t.net_pnl.to_string(),
                "entry_timestamp_ns": t.entry_timestamp_ns, "exit_timestamp_ns": t.exit_timestamp_ns,
                "gross_return_bps": t.return_bps(false).expect("validated closed-trade return").map(|n| n.to_string()),
                "net_return_bps": t.return_bps(true).expect("validated closed-trade return").map(|n| n.to_string()),
            })).collect())
    }
}

/// Execution instructions, in callback order. Targets are reconciled by the engine.
#[derive(Clone, Debug)]
pub enum OrderAction {
    Submit(EngineOrder),
    Cancel { order_id: String },
    TargetPosition { symbol: String, quantity: Decimal },
}

/// The typed failure every non-bar runtime raises for a bar event.
pub const RUNTIME_BARS_UNSUPPORTED: &str =
    "this runtime does not consume bars: bars are only dispatched to study runtimes";

/// The Python plugin seam the engine drives.
pub trait StrategyRuntime {
    /// Opt in to the engine-owned state view. Observation-only studies never opt in.
    fn wants_execution_state(&self) -> bool {
        false
    }

    fn update_execution_state(&mut self, _state: &ExecutionSnapshot) -> Result<(), BacktestError> {
        Ok(())
    }

    /// Native order-only runtimes use the same execution path as authored actions.
    fn on_frame_actions(
        &mut self,
        frame: &EngineBarFrame,
    ) -> Result<Vec<OrderAction>, BacktestError> {
        self.on_frame_orders(frame)
            .map(|orders| orders.into_iter().map(OrderAction::Submit).collect())
    }

    fn on_market_actions(
        &mut self,
        event: &EngineMarket,
    ) -> Result<Vec<OrderAction>, BacktestError> {
        let orders = match event {
            EngineMarket::Bar(bar) => self.on_bar(bar)?,
            _ => self.on_market(event)?,
        };
        Ok(orders.into_iter().map(OrderAction::Submit).collect())
    }
    /// Exact own and indicator requirements. `None` is for legacy native mocks.
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        None
    }

    /// Observe an atomic frame and return strategy intents.
    ///
    /// # Errors
    /// Propagates callback failures. Studies must never return orders.
    fn on_frame_orders(
        &mut self,
        frame: &EngineBarFrame,
    ) -> Result<Vec<EngineOrder>, BacktestError> {
        self.on_bar_frame(frame)?;
        Ok(Vec::new())
    }
    /// Observe one atomic study frame. Strategies reject frames by default.
    ///
    /// # Errors
    /// Returns an execution error for non-study runtimes or failed observations.
    fn on_bar_frame(&mut self, frame: &EngineBarFrame) -> Result<(), BacktestError> {
        let _ = frame;
        Err(BacktestError::StrategyExecution(
            RUNTIME_BARS_UNSUPPORTED.to_string(),
        ))
    }
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
    /// Called for every OHLCV bar; return the orders it submitted.
    ///
    /// Only runtimes that consume bars override this. The default fails loud
    /// so a bar can never be silently dropped or synthesized into a tick.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError`] when the runtime does not consume bars or the
    /// runtime raises.
    fn on_bar(&mut self, bar: &EngineBar) -> Result<Vec<EngineOrder>, BacktestError> {
        let _ = bar;
        Err(BacktestError::StrategyExecution(
            RUNTIME_BARS_UNSUPPORTED.to_string(),
        ))
    }
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
    /// Validated structured exploratory outputs, keyed by authored output name.
    fn published_outputs(&self) -> BTreeMap<String, serde_json::Value> {
        BTreeMap::new()
    }
}
