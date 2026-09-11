//! Numeric kernels for the quant-harness Rust engine.
//!
//! The first resident is [`Decimal18`], the scale-18 fixed-point type that
//! mirrors the pinned native kernel in `src/harness/native/decimal.zig` and the
//! exact-decimal contract enforced by `harness.core.types.common`. It is an
//! `i128` unscaled value at scale 18 (`decimal128(38, 18)`) with checked
//! arithmetic, explicit rounding modes, and allocation-free hot paths.
//!
//! [`PyDecimal`] is the variable-scale companion that reproduces Python's
//! `decimal.Decimal`: it preserves the context-visible scale and formatting
//! contract needed for byte-identical artifacts.

mod decimal18;
mod pydecimal;
mod wide;

pub use decimal18::{
    Decimal18, DecimalError, RoundingMode, MAX_UNSCALED, MIN_UNSCALED, ONE_UNSCALED, SCALE,
};
pub use pydecimal::{Context, PyDecimal, DEFAULT_PRECISION, INDICATOR_PRECISION};
