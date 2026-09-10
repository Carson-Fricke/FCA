//! The paper-trading shell seam.
//!
//! The full Python `harness.runtime.paper` shell composes replay, the paper
//! venue, the risk pipeline, and the portfolio hub. Those capabilities are
//! owned by other migration epics, so the staging crate exposes the shell's
//! supervision surface as a trait plus the deterministic session policy and
//! options records. The state store captures through this surface.

use crate::backtest::errors::{AbortPolicy, FillModel, StrategyHaltPolicy, TeardownPolicy};
use crate::backtest::fills::FillModelOptions;

/// Session boundary policy for a paper run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PaperSessionPolicy {
    /// One continuous session.
    Continuous,
    /// Stop at the configured session close.
    Session,
}

impl PaperSessionPolicy {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Continuous => "continuous",
            Self::Session => "session",
        }
    }

    /// Parse a session policy from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "continuous" => Some(Self::Continuous),
            "session" => Some(Self::Session),
            _ => None,
        }
    }
}

/// Deterministic options for one paper run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PaperOptions {
    /// Run identifier.
    pub run_id: String,
    /// String-exact initial cash.
    pub initial_cash: String,
    /// Selected fill model.
    pub fill_model: FillModel,
    /// Fill model options.
    pub fill_options: Option<FillModelOptions>,
    /// String-exact fee precision.
    pub fee_precision: String,
    /// Abort policy.
    pub abort_policy: AbortPolicy,
    /// Strategy-halt policy.
    pub strategy_halt_policy: StrategyHaltPolicy,
    /// Teardown policy.
    pub teardown_policy: TeardownPolicy,
    /// Session boundary policy.
    pub session_policy: PaperSessionPolicy,
}

impl PaperOptions {
    /// Build options with the Python defaults for one run.
    #[must_use]
    pub fn new(run_id: impl Into<String>) -> Self {
        Self {
            run_id: run_id.into(),
            initial_cash: "1000000".to_string(),
            fill_model: FillModel::LatencySize,
            fill_options: None,
            fee_precision: "0.0001".to_string(),
            abort_policy: AbortPolicy::Raise,
            strategy_halt_policy: StrategyHaltPolicy::HaltStrategy,
            teardown_policy: TeardownPolicy::Always,
            session_policy: PaperSessionPolicy::Session,
        }
    }
}

/// One paper session's terminal result, as seen by supervision.
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct PaperSessionResult {
    /// Events dispatched on the bus.
    pub dispatched_count: u64,
    /// Market events consumed.
    pub market_event_count: u64,
    /// Fills produced.
    pub fill_count: u64,
}

/// The shell supervision surface the state store captures through.
pub trait PaperShell {
    /// The shell's run identifier.
    fn run_id(&self) -> &str;
    /// Orders submitted by one strategy instance.
    fn order_count(&self, instance_id: &str) -> u64;
    /// String-exact daily loss for one strategy instance.
    fn daily_loss(&self, instance_id: &str) -> String;
    /// The current session result.
    fn result(&self) -> PaperSessionResult;
    /// Exported strategy state values, keyed by instance id.
    fn strategy_state_export(
        &self,
    ) -> std::collections::BTreeMap<String, crate::canonical_json::JsonValue>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_policy_round_trips() {
        for policy in [PaperSessionPolicy::Continuous, PaperSessionPolicy::Session] {
            assert_eq!(PaperSessionPolicy::parse(policy.value()), Some(policy));
        }
    }

    #[test]
    fn options_defaults_match_python() {
        let options = PaperOptions::new("run");
        assert_eq!(options.initial_cash, "1000000");
        assert_eq!(options.abort_policy, AbortPolicy::Raise);
        assert_eq!(options.session_policy, PaperSessionPolicy::Session);
    }
}
