//! Owns checked single-position notional caps shared by replay and live risk.
//! Multi-leg portfolio constraints remain with the portfolio risk rules.
use crate::ledger::Money;
use crate::orders::Side;
use num::DecimalError;
use std::cmp::Ordering::{Greater, Less};
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Cap {
    Order,
    Position,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Decision<T> {
    Allow,
    Reduce(T),
    Reject,
}
impl Cap {
    pub const fn name(self) -> &'static str {
        match self {
            Self::Order => "max_order_notional",
            Self::Position => "max_position_notional",
        }
    }
}
pub fn evaluate<T: Money>(
    cap: Cap,
    quantity: T,
    side: Side,
    price: T,
    current: T,
    limit: T,
) -> Result<Decision<T>, DecimalError> {
    if price.compare(T::ZERO) != Greater {
        return Ok(Decision::Reject);
    }
    let projected = |q: T| match cap {
        Cap::Order => Ok(q),
        Cap::Position => current
            .add(if side == Side::Buy { q } else { q.negated() })
            .map(Money::abs),
    };
    if projected(quantity)?.mul(price)?.compare(limit) != Greater {
        return Ok(Decision::Allow);
    }
    if limit.compare(T::ZERO) == Less {
        return Ok(Decision::Reject);
    }
    let cap_quantity = limit.div_down(price)?;
    let allowed = match cap {
        Cap::Order => cap_quantity,
        Cap::Position => {
            if side == Side::Buy {
                cap_quantity.sub(current)?
            } else {
                cap_quantity.add(current)?
            }
        }
    };
    if allowed.compare(T::ZERO) != Greater
        || allowed.compare(quantity) != Less
        || projected(allowed)?.mul(price)?.compare(limit) == Greater
    {
        return Ok(Decision::Reject);
    }
    Ok(Decision::Reduce(allowed))
}
