//! Embedded Python host for user strategies, indicators, and screeners.
//!
//! This crate is the PyO3 boundary between the Rust engine and user-authored
//! Python code. It embeds one interpreter and exposes four host surfaces:
//!
//! * [`StrategyHost`] loads unmodified strategy classes from `strategies/`,
//!   instantiates them with validated parameters (including the
//!   `parameters=` mapping convention), and dispatches lifecycle callbacks.
//! * [`IndicatorHost`] loads and calls user-authored Python indicators with a
//!   batched hot path.
//! * [`ScreenerHost`] executes user screener programs and resolves their
//!   `screen` entry point.
//! * [`PyEngineContext`] is the native, Rust-owned strategy context the engine
//!   hands to a Python strategy. It exposes clock, universe, books, orders,
//!   state, seeded random, emissions, and logging with the same attribute and
//!   method names as the historical Python context, so user strategies run
//!   unmodified without importing the Python engine.
//!
//! Every surface performs its Python work inside a single interpreter
//! acquisition per call (or per batch), so hot loops do not pay a per-event
//! GIL acquisition.

pub mod backtest;
pub mod binding;
pub mod context;
pub mod contracts;
pub mod error;
pub mod indicator;
pub mod lint;
pub mod python;
pub mod reference;
pub mod screener;
pub mod strategy;

pub use binding::StrategyBinding;
pub use context::{PyBooks, PyClock, PyEmissions, PyEngineContext, PyLog, PyOrders, PyStateStore};
pub use contracts::{
    callback_name_for_event, is_known_callback, require_callbacks, validate_callback_order,
    ContractError, ContractErrorKind, CALLBACK_ORDER,
};
pub use error::HostError;
pub use indicator::{IndicatorHost, LoadedCallable};
pub use lint::{
    LintReport, SandboxViolation, StrategyLintError, StrategyLinter, ViolationCategory,
};
pub use reference::ReferenceStrategy;
pub use screener::{LoadedScreener, ScreenerHost, SCREEN_ENTRYPOINT};
pub use strategy::{LoadedStrategy, StrategyEvent, StrategyHost, StrategySpec};

#[cfg(test)]
#[path = "../tests/unit/lib.rs"]
mod tests;
