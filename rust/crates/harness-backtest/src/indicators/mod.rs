//! Built-in tick indicators and the typed indicator registry.
//!
//! Port of `harness.indicators`. The shared bucket/session sample-emission state
//! machine, the SMA/EMA/RSI/mid-SMA/ATR/realized-volatility/VWAP/dollar-volume/
//! OBV kernels, and the typed registry that validates declarative requests
//! before construction are all ported here.

pub mod base;
pub mod momentum;
pub mod overlays;
pub mod registry;
pub mod volatility;
pub mod volume;

pub use base::{
    add, bucket_start_ns, divide, hundred, multiply, session_start_ns, sqrt, subtract,
    IndicatorError, IndicatorKernel, IndicatorMachine, IndicatorSample, OrderGuard,
    RollingWindow, Tick, TickDriver, TickIndicator, TickKind, DAY_NS, DEFAULT_BUCKET_NS,
    MIDNIGHT_UTC_NS,
};
pub use momentum::{RelativeStrengthIndex, RsiKernel};
pub use overlays::{
    EmaKernel, ExponentialMovingAverage, MidSimpleMovingAverage, MidSmaKernel,
    SimpleMovingAverage, SmaKernel,
};
pub use registry::{
    create_indicator, default_registry, indicator_names, parse_duration_ns, ConfiguredIndicator,
    IndicatorKind, IndicatorParam, IndicatorParamKind, IndicatorRegistry, IndicatorSpec,
    ParamValue,
};
pub use volatility::{AtrKernel, AverageTrueRange, RealizedVolatility, RealizedVolKernel};
pub use volume::{
    DollarVolume, DollarVolumeKernel, ObvKernel, OnBalanceVolume, VolumeWeightedAveragePrice,
    VwapKernel,
};
