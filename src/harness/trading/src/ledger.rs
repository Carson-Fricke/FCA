//! Owns checked average-cost fill accounting shared by replay and live books.
//! Callers commit the returned transition atomically and own identity/deduplication.
use num::Decimal;
use num::Decimal18;
use num::DecimalError;
use std::cmp::Ordering;

/// Checked arithmetic over the two current exact-decimal representations.
/// Operations must preserve their representation's numeric bounds and rounding.
pub trait Money: Copy {
    const ZERO: Self;
    fn compare(self, other: Self) -> Ordering;
    fn is_whole(self) -> bool;
    fn abs(self) -> Self;
    fn negated(self) -> Self;
    fn add(self, other: Self) -> Result<Self, DecimalError>;
    fn sub(self, other: Self) -> Result<Self, DecimalError>;
    fn mul(self, other: Self) -> Result<Self, DecimalError>;
    fn div(self, other: Self) -> Result<Self, DecimalError>;
    /// Divide toward zero so a positive risk allowance never rounds above its cap.
    fn div_down(self, other: Self) -> Result<Self, DecimalError>;
}

macro_rules! money {
    ($type:ty, $compare:expr, $down:expr, $whole:expr) => {
        impl Money for $type {
            const ZERO: Self = Self::ZERO;
            fn compare(self, other: Self) -> Ordering {
                ($compare)(self, other)
            }
            fn is_whole(self) -> bool {
                ($whole)(self)
            }
            fn abs(self) -> Self {
                self.abs()
            }
            fn negated(self) -> Self {
                -self
            }
            fn add(self, other: Self) -> Result<Self, DecimalError> {
                self.checked_add(other)
            }
            fn sub(self, other: Self) -> Result<Self, DecimalError> {
                self.checked_sub(other)
            }
            fn mul(self, other: Self) -> Result<Self, DecimalError> {
                self.checked_mul(other)
            }
            fn div_down(self, other: Self) -> Result<Self, DecimalError> {
                ($down)(self, other)
            }
            fn div(self, other: Self) -> Result<Self, DecimalError> {
                self.checked_div(other)
            }
        }
    };
}
money!(
    Decimal,
    |a: Decimal, b: Decimal| a.cmp_value(b),
    |a: Decimal, b: Decimal| a
        .checked_div_with(b, &num::Context::new(28, num::RoundingMode::Truncate)),
    |value: Decimal| {
        let mut scale = value.scale();
        let mut coefficient = value.coefficient();
        if coefficient == 0 {
            return true;
        }
        while scale > 0 && coefficient.is_multiple_of(10) {
            coefficient /= 10;
            scale -= 1;
        }
        scale <= 0
    }
);
money!(
    Decimal18,
    |a: Decimal18, b: Decimal18| a.cmp(&b),
    |a: Decimal18, b: Decimal18| a.checked_div_with(b, num::RoundingMode::Truncate),
    |value: Decimal18| value.unscaled() % num::ONE_UNSCALED == 0
);

/// Passive accounting state; it becomes authoritative only when the book commits it.
#[derive(Clone, Copy, Debug)]
pub struct Position<T> {
    pub quantity: T,
    pub cost_basis: T,
    pub cash_flow: T,
    pub realized_pnl: T,
}

impl<T: Money> Default for Position<T> {
    fn default() -> Self {
        Self {
            quantity: T::ZERO,
            cost_basis: T::ZERO,
            cash_flow: T::ZERO,
            realized_pnl: T::ZERO,
        }
    }
}

/// Calculate everything before mutation. Failure never produces a partial fill.
pub fn apply<T: Money>(
    cash: T,
    current: Position<T>,
    signed: T,
    price: T,
    fee: T,
) -> Result<(T, Position<T>), DecimalError> {
    let Position {
        quantity,
        cost_basis,
        cash_flow,
        mut realized_pnl,
    } = current;
    let next_quantity = quantity.add(signed)?;
    let mut next_basis = if quantity.compare(T::ZERO) == Ordering::Equal
        || quantity.compare(T::ZERO) == signed.compare(T::ZERO)
    {
        cost_basis.add(signed.mul(price)?)?
    } else if signed.abs().compare(quantity.abs()) != Ordering::Greater {
        let released = if signed.abs().compare(quantity.abs()) == Ordering::Equal {
            cost_basis
        } else {
            cost_basis.mul(signed.abs())?.div(quantity.abs())?
        };
        realized_pnl = realized_pnl.add(signed.negated().mul(price)?.sub(released)?)?;
        cost_basis.sub(released)?
    } else {
        realized_pnl = realized_pnl.add(quantity.mul(price)?.sub(cost_basis)?)?;
        next_quantity.mul(price)?
    };
    realized_pnl = realized_pnl.sub(fee)?;
    let notional = signed.mul(price)?;
    let next_flow = cash_flow.sub(notional)?.sub(fee)?;
    let next_cash = cash.sub(notional)?.sub(fee)?;
    if next_quantity.compare(T::ZERO) == Ordering::Equal {
        next_basis = T::ZERO;
        realized_pnl = next_flow;
    }
    Ok((
        next_cash,
        Position {
            quantity: next_quantity,
            cost_basis: next_basis,
            cash_flow: next_flow,
            realized_pnl,
        },
    ))
}
