//! Backtest error taxonomy and abort, halt, teardown, and status policies.
//!
//! Port of `harness.runtime.backtest.errors`. Shared exact-arithmetic
//! constants also live here so every higher-level submodule can import them.

use std::fmt;

/// One day in nanoseconds.
pub const ONE_DAY_NS: i64 = 86_400_000_000_000;

/// One microsecond in nanoseconds.
pub const MICROSECOND_NS: i64 = 1_000;

/// Base class for every deterministic backtest runner error.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BacktestError {
    /// The supplied configuration cannot drive a backtest.
    Configuration(String),
    /// Local market data failed runnability assurance.
    DataPreflight(String),
    /// A configured strategy implementation cannot be loaded.
    StrategyLoad(String),
    /// A strategy raised an unexpected exception during a run.
    StrategyExecution(String),
    /// A strategy read data newer than the engine clock head.
    LookaheadViolation(String),
    /// The run was aborted and the abort propagated.
    Aborted(String),
    /// A strategy requested that only that strategy stops.
    StrategyHalt(String),
    /// A strategy requested that the whole run stops.
    BacktestAbort(String),
    /// Exact decimal arithmetic left the decimal128(38, 18) bound, so a
    /// quantity, notional, or risk value cannot be represented.
    Arithmetic(String),
    /// A booked fill cannot be represented in the canonical accounting ledger.
    MalformedFill { fill_id: String, detail: String },
}

impl fmt::Display for BacktestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Configuration(message)
            | Self::DataPreflight(message)
            | Self::StrategyLoad(message)
            | Self::StrategyExecution(message)
            | Self::LookaheadViolation(message)
            | Self::Aborted(message)
            | Self::StrategyHalt(message)
            | Self::BacktestAbort(message)
            | Self::Arithmetic(message) => formatter.write_str(message),
            Self::MalformedFill { fill_id, detail } => {
                write!(formatter, "fill {fill_id} is malformed: {detail}")
            }
        }
    }
}

impl std::error::Error for BacktestError {}

/// What the runner does once a run has been aborted.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum AbortPolicy {
    /// Propagate the abort as an error.
    #[default]
    Raise,
    /// Stop the loop and return an aborted result.
    Stop,
}

impl AbortPolicy {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Raise => "raise",
            Self::Stop => "stop",
        }
    }

    /// Parse an abort policy from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "raise" => Some(Self::Raise),
            "stop" => Some(Self::Stop),
            _ => None,
        }
    }
}

/// What the runner does when one strategy requests a halt.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum StrategyHaltPolicy {
    /// Halt only the requesting strategy.
    #[default]
    HaltStrategy,
    /// Abort the whole run.
    AbortRun,
    /// Propagate the halt as an error.
    Raise,
}

impl StrategyHaltPolicy {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::HaltStrategy => "halt_strategy",
            Self::AbortRun => "abort_run",
            Self::Raise => "raise",
        }
    }

    /// Parse a halt policy from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "halt_strategy" => Some(Self::HaltStrategy),
            "abort_run" => Some(Self::AbortRun),
            "raise" => Some(Self::Raise),
            _ => None,
        }
    }
}

/// When the runner invokes strategy `on_stop` callbacks.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum TeardownPolicy {
    /// Always run teardown.
    #[default]
    Always,
    /// Run teardown only on success.
    OnSuccess,
    /// Never run teardown.
    Never,
}

impl TeardownPolicy {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Always => "always",
            Self::OnSuccess => "on_success",
            Self::Never => "never",
        }
    }

    /// Parse a teardown policy from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "always" => Some(Self::Always),
            "on_success" => Some(Self::OnSuccess),
            "never" => Some(Self::Never),
            _ => None,
        }
    }
}

/// Terminal status of one backtest run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BacktestStatus {
    /// The run replayed its complete fixture.
    Completed,
    /// The run stopped early.
    Aborted,
}

impl BacktestStatus {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Completed => "completed",
            Self::Aborted => "aborted",
        }
    }
}

/// Pessimistic fill plugins selectable by name.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum FillModel {
    /// Fill at the first visible touch.
    #[default]
    SimpleTouch,
    /// Require a qualifying print for a resting limit.
    LatencySize,
    /// Strictest variant; never fills from a quote alone.
    TradeThrough,
}

impl FillModel {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::SimpleTouch => "simple_touch",
            Self::LatencySize => "latency_size",
            Self::TradeThrough => "trade_through",
        }
    }

    /// Parse a fill model from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "simple_touch" => Some(Self::SimpleTouch),
            "latency_size" => Some(Self::LatencySize),
            "trade_through" => Some(Self::TradeThrough),
            _ => None,
        }
    }
}
