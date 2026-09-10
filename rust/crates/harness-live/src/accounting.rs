//! Immutable execution, fill, position, and account records.
//!
//! Mirrors the deterministic accounting types from
//! `harness.core.types.accounting`; book/account types arrive with the
//! portfolio-accounting port.

use harness_num::Decimal18;

use crate::error::{require_text, LiveError, LiveResult};
use crate::orders::{ExecutionType, OrderStatus, Side};
use crate::symbol::Symbol;

/// A venue/OMS report; fills are represented by [`Fill`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExecutionReport {
    /// Stable report identifier.
    pub report_id: String,
    /// The order this report mutates.
    pub order_id: String,
    /// Declared lifecycle status.
    pub status: OrderStatus,
    /// The lifecycle mutation kind.
    pub execution_type: ExecutionType,
    /// The submitted order quantity.
    pub order_quantity: Decimal18,
    /// Cumulative filled quantity.
    pub filled_quantity: Decimal18,
    /// Cumulative remaining quantity.
    pub remaining_quantity: Decimal18,
    /// Cumulative average fill price.
    pub average_fill_price: Option<Decimal18>,
    /// Quantity of this report's fill, if any.
    pub last_fill_quantity: Decimal18,
    /// Price of this report's fill, if any.
    pub last_fill_price: Option<Decimal18>,
    /// Optional venue text.
    pub text: Option<String>,
}

impl ExecutionReport {
    /// Construct a validated execution report.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        report_id: &str,
        order_id: &str,
        status: OrderStatus,
        execution_type: ExecutionType,
        order_quantity: Decimal18,
        filled_quantity: Decimal18,
        remaining_quantity: Decimal18,
        average_fill_price: Option<Decimal18>,
        last_fill_quantity: Decimal18,
        last_fill_price: Option<Decimal18>,
        text: Option<String>,
    ) -> LiveResult<Self> {
        let report_id = require_text(report_id, "report_id")?;
        let order_id = require_text(order_id, "order_id")?;
        if !order_quantity.is_positive() {
            return Err(LiveError::Validation("order_quantity must be positive".into()));
        }
        if filled_quantity.is_negative() {
            return Err(LiveError::Validation(
                "filled_quantity must not be negative".into(),
            ));
        }
        if remaining_quantity.is_negative() {
            return Err(LiveError::Validation(
                "remaining_quantity must not be negative".into(),
            ));
        }
        if filled_quantity > order_quantity {
            return Err(LiveError::Validation(
                "filled_quantity cannot exceed order_quantity".into(),
            ));
        }
        if filled_quantity + remaining_quantity != order_quantity {
            return Err(LiveError::Validation(
                "filled_quantity plus remaining_quantity must equal order_quantity".into(),
            ));
        }
        if let Some(value) = average_fill_price {
            if !value.is_positive() {
                return Err(LiveError::Validation(
                    "average_fill_price must be positive".into(),
                ));
            }
        }
        if last_fill_quantity.is_negative() {
            return Err(LiveError::Validation(
                "last_fill_quantity must not be negative".into(),
            ));
        }
        if let Some(value) = last_fill_price {
            if !value.is_positive() {
                return Err(LiveError::Validation(
                    "last_fill_price must be positive".into(),
                ));
            }
        }
        let text = text.map(|value| value.trim().to_string());
        Ok(Self {
            report_id,
            order_id,
            status,
            execution_type,
            order_quantity,
            filled_quantity,
            remaining_quantity,
            average_fill_price,
            last_fill_quantity,
            last_fill_price,
            text,
        })
    }
}

/// One immutable execution fill.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Fill {
    /// Stable fill identifier.
    pub fill_id: String,
    /// The order this fill belongs to.
    pub order_id: String,
    /// Canonical symbol.
    pub symbol: Symbol,
    /// Buy or sell.
    pub side: Side,
    /// Exact fill quantity.
    pub quantity: Decimal18,
    /// Exact fill price.
    pub price: Decimal18,
    /// Fee; negative values represent liquidity rebates.
    pub fee: Decimal18,
    /// Optional liquidity role (`maker`/`taker`).
    pub liquidity: Option<String>,
}

impl Fill {
    /// Construct a validated fill.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        fill_id: &str,
        order_id: &str,
        symbol: Symbol,
        side: Side,
        quantity: Decimal18,
        price: Decimal18,
        fee: Decimal18,
        liquidity: Option<String>,
    ) -> LiveResult<Self> {
        let fill_id = require_text(fill_id, "fill_id")?;
        let order_id = require_text(order_id, "order_id")?;
        if !quantity.is_positive() {
            return Err(LiveError::Validation("quantity must be positive".into()));
        }
        if !price.is_positive() {
            return Err(LiveError::Validation("price must be positive".into()));
        }
        let liquidity = match liquidity {
            Some(value) => Some(require_text(&value, "liquidity")?),
            None => None,
        };
        Ok(Self {
            fill_id,
            order_id,
            symbol,
            side,
            quantity,
            price,
            fee,
            liquidity,
        })
    }
}

/// An account's signed position and valuation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Position {
    /// Account identifier.
    pub account_id: String,
    /// Canonical symbol.
    pub symbol: Symbol,
    /// Signed quantity.
    pub quantity: Decimal18,
    /// Average entry price.
    pub average_price: Decimal18,
    /// Realized profit and loss.
    pub realized_pnl: Decimal18,
    /// Optional market price.
    pub market_price: Option<Decimal18>,
    /// Optional unrealized profit and loss.
    pub unrealized_pnl: Option<Decimal18>,
}

impl Position {
    /// Construct a validated position.
    pub fn new(
        account_id: &str,
        symbol: Symbol,
        quantity: Decimal18,
        average_price: Decimal18,
        realized_pnl: Decimal18,
        market_price: Option<Decimal18>,
        unrealized_pnl: Option<Decimal18>,
    ) -> LiveResult<Self> {
        let account_id = require_text(account_id, "account_id")?;
        if average_price.is_negative() {
            return Err(LiveError::Validation(
                "average_price must not be negative".into(),
            ));
        }
        if let Some(value) = market_price {
            if !value.is_positive() {
                return Err(LiveError::Validation("market_price must be positive".into()));
            }
        }
        Ok(Self {
            account_id,
            symbol,
            quantity,
            average_price,
            realized_pnl,
            market_price,
            unrealized_pnl,
        })
    }
}

/// A complete account valuation snapshot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AccountSnapshot {
    /// Account identifier.
    pub account_id: String,
    /// Cash balance.
    pub cash: Decimal18,
    /// Mark-to-market equity.
    pub equity: Decimal18,
    /// Available buying power.
    pub buying_power: Decimal18,
    /// Margin used.
    pub margin_used: Decimal18,
    /// Open positions.
    pub positions: Vec<Position>,
}

impl AccountSnapshot {
    /// Construct a validated account snapshot.
    pub fn new(
        account_id: &str,
        cash: Decimal18,
        equity: Decimal18,
        buying_power: Decimal18,
        margin_used: Decimal18,
        positions: Vec<Position>,
    ) -> LiveResult<Self> {
        let account_id = require_text(account_id, "account_id")?;
        if margin_used.is_negative() {
            return Err(LiveError::Validation(
                "margin_used must not be negative".into(),
            ));
        }
        Ok(Self {
            account_id,
            cash,
            equity,
            buying_power,
            margin_used,
            positions,
        })
    }
}

#[cfg(test)]
mod tests {
    use harness_num::Decimal18;

    use super::{ExecutionReport, Fill};
    use crate::orders::{ExecutionType, OrderStatus, Side};
    use crate::symbol::Symbol;

    fn dec(value: &str) -> Decimal18 {
        value.parse().unwrap()
    }

    #[test]
    fn execution_report_validates_quantity_identity() {
        let report = ExecutionReport::new(
            "r1",
            "o1",
            OrderStatus::Accepted,
            ExecutionType::Acknowledged,
            dec("10"),
            dec("0"),
            dec("10"),
            None,
            dec("0"),
            None,
            None,
        );
        assert!(report.is_ok());
        let bad = ExecutionReport::new(
            "r1",
            "o1",
            OrderStatus::Accepted,
            ExecutionType::Acknowledged,
            dec("10"),
            dec("0"),
            dec("9"),
            None,
            dec("0"),
            None,
            None,
        );
        assert!(bad.is_err());
    }

    #[test]
    fn fill_allows_negative_rebate_fee() {
        let fill = Fill::new(
            "f1",
            "o1",
            Symbol::new("AAPL").unwrap(),
            Side::Buy,
            dec("1"),
            dec("100"),
            dec("-0.01"),
            Some("maker".into()),
        );
        assert!(fill.is_ok());
    }
}
