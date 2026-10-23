//! Immutable single- and multi-leg order records.
//!
//! Mirrors `harness.core.types.orders` with the event envelope (wall-clock
//! identity) deliberately omitted: the synchronous live kernel only observes
//! deterministic domain fields, and the Python engine's `event_id`/`timestamp`
//! ordering fields are reconstructed by the async runtime.

use num::Decimal18;

use crate::error::require_text;
use crate::error::LiveError;
use crate::error::LiveResult;
use crate::symbol::Symbol;

/// Buy or sell.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Side {
    /// Buy.
    Buy,
    /// Sell.
    Sell,
}

impl Side {
    /// The canonical wire spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Buy => "buy",
            Self::Sell => "sell",
        }
    }

    /// The opposite side.
    pub const fn opposite(self) -> Self {
        match self {
            Self::Buy => Self::Sell,
            Self::Sell => Self::Buy,
        }
    }
}

/// A price constraint on an order.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum OrderType {
    /// Market.
    Market,
    /// Limit.
    Limit,
    /// Stop.
    Stop,
    /// Stop-limit.
    StopLimit,
}

impl OrderType {
    /// The canonical wire spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Market => "market",
            Self::Limit => "limit",
            Self::Stop => "stop",
            Self::StopLimit => "stop_limit",
        }
    }
}

/// How long an order remains working.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum TimeInForce {
    /// Day.
    Day,
    /// Good-till-canceled.
    Gtc,
    /// Immediate-or-cancel.
    Ioc,
}

impl TimeInForce {
    /// The canonical wire spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Day => "day",
            Self::Gtc => "gtc",
            Self::Ioc => "ioc",
        }
    }
}

/// The lifecycle state of an order.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum OrderStatus {
    /// Locally new.
    New,
    /// Venue acknowledged.
    Accepted,
    /// Partially filled.
    PartiallyFilled,
    /// Fully filled.
    Filled,
    /// Cancel requested, not yet confirmed.
    CancelPending,
    /// Canceled.
    Canceled,
    /// Rejected.
    Rejected,
    /// Expired.
    Expired,
    /// Replaced.
    Replaced,
}

impl OrderStatus {
    /// The canonical wire spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::New => "new",
            Self::Accepted => "accepted",
            Self::PartiallyFilled => "partially_filled",
            Self::Filled => "filled",
            Self::CancelPending => "cancel_pending",
            Self::Canceled => "canceled",
            Self::Rejected => "rejected",
            Self::Expired => "expired",
            Self::Replaced => "replaced",
        }
    }
}

/// The kind of lifecycle mutation carried by an execution report.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ExecutionType {
    /// Acknowledgement.
    Acknowledged,
    /// Partial fill.
    PartialFill,
    /// Complete fill.
    Fill,
    /// Canceled.
    Canceled,
    /// Rejected.
    Rejected,
    /// Expired.
    Expired,
    /// Replaced.
    Replaced,
    /// Status heartbeat.
    Status,
}

impl ExecutionType {
    /// The canonical wire spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Acknowledged => "acknowledged",
            Self::PartialFill => "partial_fill",
            Self::Fill => "fill",
            Self::Canceled => "canceled",
            Self::Rejected => "rejected",
            Self::Expired => "expired",
            Self::Replaced => "replaced",
            Self::Status => "status",
        }
    }
}

/// One signed leg in a multi-leg order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OrderLeg {
    /// The canonical leg symbol.
    pub symbol: Symbol,
    /// Buy or sell.
    pub side: Side,
    /// Exact leg quantity.
    pub quantity: Decimal18,
    /// Ratio used for group net-price accounting.
    pub ratio: Decimal18,
    /// Optional venue-facing leg identifier.
    pub leg_id: Option<String>,
}

impl OrderLeg {
    /// Construct a validated leg.
    pub fn new(
        symbol: Symbol,
        side: Side,
        quantity: Decimal18,
        ratio: Decimal18,
        leg_id: Option<String>,
    ) -> LiveResult<Self> {
        if !quantity.is_positive() {
            return Err(LiveError::Validation("quantity must be positive".into()));
        }
        if !ratio.is_positive() {
            return Err(LiveError::Validation("ratio must be positive".into()));
        }
        let leg_id = match leg_id {
            Some(value) => Some(require_text(&value, "leg_id")?),
            None => None,
        };
        Ok(Self {
            symbol,
            side,
            quantity,
            ratio,
            leg_id,
        })
    }
}

/// A single-leg order for one symbol.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SingleLegOrder {
    /// Stable order identifier.
    pub order_id: String,
    /// Canonical symbol.
    pub symbol: Symbol,
    /// Buy or sell.
    pub side: Side,
    /// Exact quantity.
    pub quantity: Decimal18,
    /// Price constraint.
    pub order_type: OrderType,
    /// Time-in-force policy.
    pub time_in_force: TimeInForce,
    /// Limit price, when required.
    pub limit_price: Option<Decimal18>,
    /// Stop price, when required.
    pub stop_price: Option<Decimal18>,
    /// Optional client order identifier.
    pub client_order_id: Option<String>,
}

impl SingleLegOrder {
    /// Construct a validated single-leg order.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        order_id: &str,
        symbol: Symbol,
        side: Side,
        quantity: Decimal18,
        order_type: OrderType,
        time_in_force: TimeInForce,
        limit_price: Option<Decimal18>,
        stop_price: Option<Decimal18>,
        client_order_id: Option<String>,
    ) -> LiveResult<Self> {
        let order_id = require_text(order_id, "order_id")?;
        if !quantity.is_positive() {
            return Err(LiveError::Validation("quantity must be positive".into()));
        }
        for (value, field) in [(limit_price, "limit_price"), (stop_price, "stop_price")] {
            if let Some(value) = value {
                if !value.is_positive() {
                    return Err(LiveError::Validation(format!("{field} must be positive")));
                }
            }
        }
        let expected = match order_type {
            OrderType::Market => (false, false),
            OrderType::Limit => (true, false),
            OrderType::Stop => (false, true),
            OrderType::StopLimit => (true, true),
        };
        if (limit_price.is_some(), stop_price.is_some()) != expected {
            return Err(LiveError::Validation(format!(
                "{} order has invalid limit_price/stop_price",
                order_type.as_str()
            )));
        }
        let client_order_id = match client_order_id {
            Some(value) => Some(require_text(&value, "client_order_id")?),
            None => None,
        };
        Ok(Self {
            order_id,
            symbol,
            side,
            quantity,
            order_type,
            time_in_force,
            limit_price,
            stop_price,
            client_order_id,
        })
    }

    /// Short order-type alias used by strategy code.
    pub const fn order_type(&self) -> OrderType {
        self.order_type
    }
}

/// An atomic order containing two or more independently signed legs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MultiLegOrder {
    /// Stable order identifier.
    pub order_id: String,
    /// Ordered legs.
    pub legs: Vec<OrderLeg>,
    /// Price constraint.
    pub order_type: OrderType,
    /// Time-in-force policy.
    pub time_in_force: TimeInForce,
    /// Optional client order identifier.
    pub client_order_id: Option<String>,
    /// Net limit price, required for limit groups.
    pub limit_price: Option<Decimal18>,
}

impl MultiLegOrder {
    /// Construct a validated multi-leg order.
    pub fn new(
        order_id: &str,
        legs: Vec<OrderLeg>,
        order_type: OrderType,
        time_in_force: TimeInForce,
        client_order_id: Option<String>,
        limit_price: Option<Decimal18>,
    ) -> LiveResult<Self> {
        let order_id = require_text(order_id, "order_id")?;
        if legs.len() < 2 {
            return Err(LiveError::Validation(
                "multi-leg order requires at least two OrderLeg values".into(),
            ));
        }
        if matches!(order_type, OrderType::Stop | OrderType::StopLimit) {
            return Err(LiveError::Validation(
                "stop orders are not supported for multi-leg orders".into(),
            ));
        }
        if let Some(value) = limit_price {
            if !value.is_positive() {
                return Err(LiveError::Validation("limit_price must be positive".into()));
            }
        }
        match order_type {
            OrderType::Limit if limit_price.is_none() => {
                return Err(LiveError::Validation(
                    "limit multi-leg order requires a limit_price".into(),
                ));
            }
            OrderType::Market if limit_price.is_some() => {
                return Err(LiveError::Validation(
                    "market multi-leg order cannot have a limit_price".into(),
                ));
            }
            _ => {}
        }
        let client_order_id = match client_order_id {
            Some(value) => Some(require_text(&value, "client_order_id")?),
            None => None,
        };
        Ok(Self {
            order_id,
            legs,
            order_type,
            time_in_force,
            client_order_id,
            limit_price,
        })
    }
}

/// Either kind of order a venue accepts.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OrderRequest {
    /// A single-leg order.
    Single(SingleLegOrder),
    /// A multi-leg group.
    Multi(MultiLegOrder),
}

impl OrderRequest {
    /// The order identifier.
    pub fn order_id(&self) -> &str {
        match self {
            Self::Single(order) => &order.order_id,
            Self::Multi(order) => &order.order_id,
        }
    }

    /// The single-leg payload, when present.
    pub fn as_single(&self) -> Option<&SingleLegOrder> {
        match self {
            Self::Single(order) => Some(order),
            Self::Multi(_) => None,
        }
    }

    /// The multi-leg payload, when present.
    pub fn as_multi(&self) -> Option<&MultiLegOrder> {
        match self {
            Self::Multi(order) => Some(order),
            Self::Single(_) => None,
        }
    }
}
