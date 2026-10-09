//! Exact fill options shared with paper execution.

/// Whether a partially filled resting order's remainder rests or is canceled.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum RemainderPolicy {
    /// Keep the remainder resting.
    #[default]
    Rest,
    /// Cancel the remainder after a partial fill.
    Cancel,
}

impl RemainderPolicy {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Rest => "rest",
            Self::Cancel => "cancel",
        }
    }

    /// Parse a remainder policy from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "rest" => Some(Self::Rest),
            "cancel" => Some(Self::Cancel),
            _ => None,
        }
    }
}

/// Exact, deterministic options shared by the pessimistic fill plugins.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FillModelOptions {
    /// Deterministic response delay in nanoseconds.
    pub response_latency_ns: i64,
    /// Partial-fill remainder handling.
    pub remainder_policy: RemainderPolicy,
    /// Whether a resting limit requires a trade-through print.
    pub require_trade_through: bool,
    /// String-exact minimum trade-through distance.
    pub minimum_trade_through: String,
    /// Whether the trade-through model is strict.
    pub strict: bool,
}

impl Default for FillModelOptions {
    fn default() -> Self {
        Self {
            response_latency_ns: 0,
            remainder_policy: RemainderPolicy::Rest,
            require_trade_through: true,
            minimum_trade_through: "0".to_string(),
            strict: true,
        }
    }
}
