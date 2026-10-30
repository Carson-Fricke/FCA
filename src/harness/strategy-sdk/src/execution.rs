//! Read-only engine accounting and concise order requests for authored strategies.
//! These views never apply fills or infer lifecycle; the harness owns that state.
use crate::{Decimal, OrderType, Side, TimeInForce};
use serde::Deserialize;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
/// Immutable booked state at the current callback boundary. Monetary values
/// use account currency; return methods produce fractions, not percentages/bps.
/// Missing marks remain `None`; submitting an order never modifies this view.
pub struct ExecutionSnapshot {
    pub timestamp_ns: i64,
    #[serde(deserialize_with = "decimal")]
    pub initial_cash: Decimal,
    #[serde(deserialize_with = "decimal")]
    pub cash: Decimal,
    #[serde(deserialize_with = "decimal")]
    pub fees: Decimal,
    #[serde(deserialize_with = "optional_decimal")]
    pub equity: Option<Decimal>,
    #[serde(deserialize_with = "decimal")]
    pub realized_pnl: Decimal,
    #[serde(deserialize_with = "optional_decimal")]
    pub unrealized_pnl: Option<Decimal>,
    #[serde(deserialize_with = "optional_decimal")]
    pub gross_exposure: Option<Decimal>,
    pub positions: Vec<Position>,
}

/// Engine-matched FIFO trade slice. Returns include both allocated fill fees
/// and use matched filled entry notional. A partial close is its own slice.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClosedTrade {
    pub trade_id: u64,
    pub symbol: String,
    pub side: String,
    pub entry_timestamp_ns: i64,
    pub exit_timestamp_ns: i64,
    #[serde(deserialize_with = "decimal")]
    pub quantity: Decimal,
    #[serde(deserialize_with = "decimal")]
    pub entry_price: Decimal,
    #[serde(deserialize_with = "decimal")]
    pub exit_price: Decimal,
    #[serde(deserialize_with = "decimal")]
    pub gross_pnl: Decimal,
    #[serde(deserialize_with = "decimal")]
    pub fees: Decimal,
    #[serde(deserialize_with = "decimal")]
    pub net_pnl: Decimal,
    #[serde(deserialize_with = "optional_decimal")]
    pub gross_return_bps: Option<Decimal>,
    #[serde(deserialize_with = "optional_decimal")]
    pub net_return_bps: Option<Decimal>,
}

impl ExecutionSnapshot {
    pub fn position(&self, symbol: &str) -> Option<&Position> {
        self.positions.iter().find(|p| p.symbol == symbol)
    }
    pub fn quantity(&self, symbol: &str) -> Decimal {
        self.position(symbol).map_or(Decimal::ZERO, |p| p.quantity)
    }
    /// Return on this instance's opening allocation, not per-trade notional.
    pub fn account_return(&self) -> Result<Option<Decimal>, num::DecimalError> {
        if self.initial_cash <= Decimal::ZERO {
            return Ok(None);
        }
        self.equity
            .map(|equity| {
                equity
                    .checked_sub(self.initial_cash)?
                    .checked_div(self.initial_cash)
            })
            .transpose()
    }
    /// Gross exposure divided by opening allocation (a fraction). Undefined
    /// when allocation is nonpositive or a mark is unavailable; checked numeric
    /// failures return `DecimalError` rather than substituting zero.
    pub fn capital_utilization(&self) -> Result<Option<Decimal>, num::DecimalError> {
        if self.initial_cash <= Decimal::ZERO {
            return Ok(None);
        }
        self.gross_exposure
            .map(|exposure| exposure.checked_div(self.initial_cash))
            .transpose()
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Position {
    pub symbol: String,
    #[serde(deserialize_with = "decimal")]
    pub quantity: Decimal,
    #[serde(deserialize_with = "decimal")]
    pub average_price: Decimal,
    #[serde(deserialize_with = "decimal")]
    pub realized_pnl: Decimal,
    #[serde(deserialize_with = "optional_decimal")]
    pub unrealized_pnl: Option<Decimal>,
    #[serde(deserialize_with = "optional_decimal")]
    pub market_value: Option<Decimal>,
}
impl Position {
    pub fn is_flat(&self) -> bool {
        self.quantity.is_zero()
    }
    pub fn total_pnl(&self) -> Result<Option<Decimal>, num::DecimalError> {
        self.unrealized_pnl
            .map(|unrealized| self.realized_pnl.checked_add(unrealized))
            .transpose()
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrderState {
    pub order_id: String,
    pub symbol: String,
    pub side: String,
    #[serde(deserialize_with = "decimal")]
    pub quantity: Decimal,
    #[serde(deserialize_with = "decimal")]
    pub filled_quantity: Decimal,
    #[serde(deserialize_with = "decimal")]
    pub remaining_quantity: Decimal,
    pub status: String,
    pub reason: Option<String>,
}
impl OrderState {
    pub fn is_terminal(&self) -> bool {
        matches!(
            self.status.as_str(),
            "filled" | "canceled" | "expired" | "rejected"
        )
    }
}

/// A request with platform-owned identity, timestamps, fills and accounting.
#[derive(Clone, Debug)]
pub struct Order {
    pub symbol: String,
    pub side: Side,
    pub quantity: Decimal,
    pub order_type: OrderType,
    pub time_in_force: TimeInForce,
    pub limit_price: Option<Decimal>,
    pub stop_price: Option<Decimal>,
}
impl Order {
    pub fn market(symbol: &str, side: Side, quantity: Decimal) -> Self {
        Self {
            symbol: symbol.into(),
            side,
            quantity,
            order_type: OrderType::Market,
            time_in_force: TimeInForce::Day,
            limit_price: None,
            stop_price: None,
        }
    }
    pub fn limit(symbol: &str, side: Side, quantity: Decimal, price: Decimal) -> Self {
        Self {
            order_type: OrderType::Limit,
            limit_price: Some(price),
            ..Self::market(symbol, side, quantity)
        }
    }
    pub fn stop(symbol: &str, side: Side, quantity: Decimal, price: Decimal) -> Self {
        Self {
            order_type: OrderType::Stop,
            stop_price: Some(price),
            ..Self::market(symbol, side, quantity)
        }
    }
    pub fn stop_limit(
        symbol: &str,
        side: Side,
        quantity: Decimal,
        stop: Decimal,
        limit: Decimal,
    ) -> Self {
        Self {
            order_type: OrderType::StopLimit,
            stop_price: Some(stop),
            limit_price: Some(limit),
            ..Self::market(symbol, side, quantity)
        }
    }
    pub fn with_time_in_force(mut self, time_in_force: TimeInForce) -> Self {
        self.time_in_force = time_in_force;
        self
    }
}

fn decimal<'de, D: serde::Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
    String::deserialize(deserializer)?
        .parse()
        .map_err(serde::de::Error::custom)
}
fn optional_decimal<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<Decimal>, D::Error> {
    Option::<String>::deserialize(deserializer)?
        .map(|s| s.parse().map_err(serde::de::Error::custom))
        .transpose()
}
