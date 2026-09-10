//! Built-in tick indicators and the typed indicator registry.
//!
//! Port of `harness.indicators`. The deterministic arithmetic context, ordering
//! guard, bucket flooring, and the SMA/EMA/RSI/mid-SMA kernels are ported with
//! the typed registry that validates declarative requests before construction.

pub mod base;
pub mod momentum;
pub mod overlays;
pub mod registry;

pub use base::{
    add, bucket_start_ns, divide, hundred, multiply, subtract, IndicatorError, IndicatorSample,
    OrderGuard, Tick, TickDriver, TickIndicator, TickKind,
};
pub use momentum::RelativeStrengthIndex;
pub use overlays::{ExponentialMovingAverage, MidSimpleMovingAverage, SimpleMovingAverage};
pub use registry::{
    create_indicator, default_registry, indicator_names, parse_duration_ns, ConfiguredIndicator,
    IndicatorKind, IndicatorParam, IndicatorParamKind, IndicatorRegistry, IndicatorSpec, ParamValue,
};
