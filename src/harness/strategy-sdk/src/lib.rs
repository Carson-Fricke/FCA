//! Authoring SDK for Rust-native harness strategies, indicators, and studies.
//!
//! This crate is the stable authoring surface that replaces the embedded
//! Python interpreter: a model authors a small Rust crate against these types
//! and traits, the platform JIT-compiles it into a `cdylib`, and the engine
//! drives it through the C ABI defined in [`abi`] behind the existing
//! [`StrategyRuntime`](crate::program::StrategyRuntime) engine seam.
//!
//! Layering:
//!
//! * [`events`] — the author-facing event and start views (borrowed strings,
//!   exact [`Decimal`]s) mirroring the engine's `EngineMarket`/`EngineFill`.
//! * [`program`] — the authoring traits ([`StudyProgram`](program::StudyProgram),
//!   [`StrategyProgram`](program::StrategyProgram), [`Indicator`](indicator::Indicator))
//!   and the capability surfaces they may touch. A study program never sees
//!   the order surface; a strategy program submits through [`OrderSink`].
//! * [`abi`] — the versioned C ABI: `#[repr(C)]` argument shapes, result
//!   codes, and the vtable contract. Rust trait objects never cross the
//!   boundary; every crossing is a plain extern function over these structs.
//! * [`export`] — the entry-point macros ([`export_study_program!`],
//!   [`export_strategy_program!`], both available at the crate root) that
//!   turn one authored type into the exported dylib symbols.
//!
//! ABI stability rules: the vtable shapes and argument structs in [`abi`]
//! only ever grow at the tail with a version bump; the loader rejects any
//! artifact whose [`SDK_ABI_VERSION`] differs from the host's. The SDK crate
//! version ([`SDK_VERSION`]) is recorded with every compiled artifact.

mod abi;
mod events;
/// Macro-support trampolines; reachable only through the exported macros.
#[doc(hidden)]
pub mod export;
mod indicator;
mod program;

pub use abi::{
    CDecimal, CFill, CMarketOrder, CMetricSink, COrderSink, CQuoteEvent, CSlice, CStart, CStr,
    CTradeEvent, MetricPublishFn, OrderSubmitFn, StrategyProgramVTable, StudyProgramVTable,
    ABI_EXPORT, LAST_ERROR_EXPORT, METRIC_INVALID_NAME, METRIC_NOT_FINITE, METRIC_OK,
    METRIC_REJECTED, METRIC_RESERVED_NAME, SDK_ABI_VERSION, SDK_ERROR, SDK_OK,
    STRATEGY_VTABLE_EXPORT, STUDY_VTABLE_EXPORT,
};
pub use events::{Fill, ProgramStart, QuoteEvent, TradeEvent};
pub use indicator::{
    indicator_add, indicator_context, indicator_div, indicator_mul, indicator_sub, Indicator,
    INDICATOR_CONTEXT_PRECISION,
};
pub use program::{
    MetricPublisher, OrderSink, OrderType, PublishError, Side, StrategyProgram, StudyProgram,
    SubmitError, SubmittedOrder, TimeInForce, RESERVED_METRIC_NAMES,
};

/// The SDK crate version, compiled into every authored artifact manifest.
pub const SDK_VERSION: &str = env!("CARGO_PKG_VERSION");

/// The exact decimal type shared with the engine and Python's `decimal.Decimal`.
///
/// Scale, coefficient, and sign are preserved exactly, so authored programs
/// compute with the same arithmetic contract the engine records.
pub use num::PyDecimal as Decimal;

/// The fixed-scale decimal kernel used by lake Arrow schemas.
pub use num::Decimal18;

/// The JSON value type authors receive as program parameters.
pub use serde_json;
