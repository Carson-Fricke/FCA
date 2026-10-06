//! Numeric kernels for the quant-harness Rust engine.
//!
//! The first resident is [`Decimal18`], the scale-18 fixed-point type that
//! mirrors the pinned native kernel and the exact-decimal contract enforced by
//! `harness.core.types.common`. It is an
//! `i128` unscaled value at scale 18 (`decimal128(38, 18)`) with checked
//! arithmetic, explicit rounding modes, and allocation-free hot paths.
//!
//! [`Decimal`] is the variable-scale companion that preserves the exact scale
//! and canonical text needed for byte-identical artifacts, and provides the
//! one-way [`Decimal::to_f64`] boundary for metrics and statistics.

mod decimal;
mod decimal18;
mod wide;

pub use decimal::{Context, Decimal, DEFAULT_PRECISION, INDICATOR_PRECISION};
pub use decimal18::{
    Decimal18, DecimalError, RoundingMode, MAX_UNSCALED, MIN_UNSCALED, ONE_UNSCALED, SCALE,
};
