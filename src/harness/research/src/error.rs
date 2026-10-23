//! Owns typed failures crossing research configuration, acquisition and publication boundaries.

use harness_trading::replay_errors::BacktestError;

/// A backtest job failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BacktestJobError {
    /// The configuration document could not be read.
    Io(String),
    /// The document does not carry a `shell` key.
    MissingShell,
    /// The shell is not `backtest`.
    WrongShell(String),
    /// The shell is not `screen`.
    WrongScreenShell(String),
    /// The engine rejected the run.
    Engine(String),
    /// A fill's quantity, price, fee, or side is malformed, or its notional
    /// leaves the decimal128(38,18) bound. The job fails; no partial result is
    /// written.
    MalformedFill {
        /// The fill that failed.
        fill_id: String,
        /// What is malformed about it.
        detail: String,
    },
    /// Exact ledger or metric arithmetic left the decimal128(38,18) bound.
    Overflow(String),
    /// An artifact could not be written.
    Artifact(String),
}

impl std::fmt::Display for BacktestJobError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(message) => write!(formatter, "{message}"),
            Self::MissingShell => formatter.write_str("configuration has no 'shell' key"),
            Self::WrongShell(shell) => {
                write!(
                    formatter,
                    "configuration shell must be 'backtest', not '{shell}'"
                )
            }
            Self::WrongScreenShell(shell) => {
                write!(
                    formatter,
                    "configuration shell must be 'screen', not '{shell}'"
                )
            }
            Self::Engine(message) => write!(formatter, "{message}"),
            Self::MalformedFill { fill_id, detail } => {
                write!(formatter, "fill {fill_id} is malformed: {detail}")
            }
            Self::Overflow(message) => write!(formatter, "{message}"),
            Self::Artifact(message) => write!(formatter, "{message}"),
        }
    }
}

impl std::error::Error for BacktestJobError {}

impl From<BacktestError> for BacktestJobError {
    fn from(error: BacktestError) -> Self {
        match error {
            BacktestError::MalformedFill { fill_id, detail } => {
                Self::MalformedFill { fill_id, detail }
            }
            other => Self::Engine(other.to_string()),
        }
    }
}
