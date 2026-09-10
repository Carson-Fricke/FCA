//! Numeric kernels for the quant-harness Rust engine.
//!
//! The first resident is [`Decimal18`], the scale-18 fixed-point type that
//! mirrors the pinned native kernel in `src/harness/native/decimal.zig` and the
//! exact-decimal contract enforced by `harness.core.types.common`. It is an
//! `i128` unscaled value at scale 18 (`decimal128(38, 18)`) with checked
//! arithmetic, explicit rounding modes, and allocation-free hot paths.
//!
//! Float64 batch quant kernels arrive in a follow-up task.

mod decimal18;
mod wide;

pub use decimal18::{
    Decimal18, DecimalError, RoundingMode, MAX_UNSCALED, MIN_UNSCALED, ONE_UNSCALED, SCALE,
};
