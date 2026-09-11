//! One exact fill decision and the immutable market event that caused it.

use num::Decimal18;

use crate::error::{require_text, LiveError, LiveResult};
use crate::events::MarketEvent;
use crate::symbol::Symbol;

/// One exact fill decision and the immutable market event that caused it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FillDecision {
    /// The order being filled.
    pub order_id: String,
    /// Fill quantity, bounded by displayed size and remaining quantity.
    pub quantity: Decimal18,
    /// Execution price.
    pub price: Decimal18,
    /// The decision price (equal to `price` under these models).
    pub decision_price: Decimal18,
    /// The market snapshot that produced the decision.
    pub triggering_market: MarketEvent,
    /// `maker` or `taker`.
    pub liquidity: String,
}

impl FillDecision {
    /// Construct a validated fill decision.
    pub fn new(
        order_id: &str,
        quantity: Decimal18,
        price: Decimal18,
        decision_price: Decimal18,
        triggering_market: MarketEvent,
        liquidity: &str,
    ) -> LiveResult<Self> {
        let order_id = require_text(order_id, "order_id")?;
        if !quantity.is_positive() {
            return Err(LiveError::Validation("quantity must be positive".into()));
        }
        if !price.is_positive() || !decision_price.is_positive() {
            return Err(LiveError::Validation(
                "price and decision_price must be positive".into(),
            ));
        }
        if liquidity != "maker" && liquidity != "taker" {
            return Err(LiveError::Validation(
                "liquidity must be maker or taker".into(),
            ));
        }
        Ok(Self {
            order_id,
            quantity,
            price,
            decision_price,
            triggering_market,
            liquidity: liquidity.to_string(),
        })
    }

    /// The execution price under the common fill vocabulary.
    pub fn fill_price(&self) -> Decimal18 {
        self.price
    }

    /// The exact market snapshot used for this decision.
    pub fn market(&self) -> &MarketEvent {
        &self.triggering_market
    }

    /// The symbol from the triggering market snapshot.
    pub fn symbol(&self) -> &Symbol {
        self.triggering_market.symbol()
    }

    /// Whether the decision represents passive liquidity.
    pub fn is_maker(&self) -> bool {
        self.liquidity == "maker"
    }
}
