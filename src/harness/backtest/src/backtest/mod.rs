//! Native deterministic backtest execution and its canonical evidence.

pub mod config;
pub mod engine_core;
pub mod event_bus;
mod execution;
pub mod fees;
pub mod fixture;
pub mod history;
pub mod identity;
pub mod manifest;
pub mod null;
pub mod outcome;
pub mod portfolio;
pub mod preflight;
pub mod projection;
pub mod risk;
pub mod rng;
pub mod screen;
pub mod stream;
pub mod subscriptions;
pub mod valuation;
pub mod venue;
pub mod yaml;
