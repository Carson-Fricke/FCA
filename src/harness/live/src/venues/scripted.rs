//! Deterministic execution venue driven by an explicit FIFO script.

use std::collections::VecDeque;

use crate::accounting::{ExecutionReport, Fill};
use crate::error::{require_text, LiveError, LiveResult};
use crate::orders::OrderRequest;

/// A scheduled venue outcome.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum VenueOutcome {
    /// An execution report.
    Report(ExecutionReport),
    /// A fill.
    Fill(Fill),
    /// A position snapshot expressed as an account identifier.
    PositionSnapshot(String),
}

/// Commands understood by an execution venue.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum VenueOperation {
    /// Submit.
    Submit,
    /// Cancel.
    Cancel,
    /// Replace.
    Replace,
    /// Position snapshot.
    PositionSnapshot,
}

impl VenueOperation {
    /// The canonical wire spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Submit => "submit",
            Self::Cancel => "cancel",
            Self::Replace => "replace",
            Self::PositionSnapshot => "position_snapshot",
        }
    }
}

/// Normalized identity of one command received by a scripted venue.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VenueCall {
    /// The command kind.
    pub operation: VenueOperation,
    /// The order identifier, for submit/cancel/replace.
    pub order_id: Option<String>,
    /// The replacement order identifier, for replace.
    pub replacement_order_id: Option<String>,
    /// The account identifier, for position snapshots.
    pub account_id: Option<String>,
}

impl VenueCall {
    /// Construct a validated call for `operation`.
    pub fn new(
        operation: VenueOperation,
        order_id: Option<String>,
        replacement_order_id: Option<String>,
        account_id: Option<String>,
    ) -> LiveResult<Self> {
        let expected = match operation {
            VenueOperation::Submit => (true, false, false),
            VenueOperation::Cancel => (true, false, false),
            VenueOperation::Replace => (true, true, false),
            VenueOperation::PositionSnapshot => (false, false, true),
        };
        let populated = (
            order_id.is_some(),
            replacement_order_id.is_some(),
            account_id.is_some(),
        );
        if populated != expected {
            return Err(LiveError::Venue(format!(
                "{} call has invalid order/account identifiers",
                operation.as_str()
            )));
        }
        let order_id = match order_id {
            Some(value) => Some(require_text(&value, "order_id")?),
            None => None,
        };
        let replacement_order_id = match replacement_order_id {
            Some(value) => Some(require_text(&value, "replacement_order_id")?),
            None => None,
        };
        let account_id = match account_id {
            Some(value) => Some(require_text(&value, "account_id")?),
            None => None,
        };
        Ok(Self {
            operation,
            order_id,
            replacement_order_id,
            account_id,
        })
    }
}

/// An expected venue call and the event outcomes it schedules.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScriptedStep {
    /// The expected command.
    pub call: VenueCall,
    /// The outcomes to schedule once matched.
    pub outcomes: Vec<VenueOutcome>,
}

/// Raised when a call does not match the next scripted step.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UnexpectedVenueCall {
    /// A human-readable explanation.
    pub message: String,
}

impl core::fmt::Display for UnexpectedVenueCall {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for UnexpectedVenueCall {}

fn message(error: LiveError) -> UnexpectedVenueCall {
    UnexpectedVenueCall {
        message: error.to_string(),
    }
}

/// Schedule prebuilt outcomes for an exact sequence of venue commands.
///
/// The script owns no clock, random source, or network resource.
#[derive(Clone, Debug, Default)]
pub struct ScriptedVenue {
    steps: VecDeque<ScriptedStep>,
    calls: Vec<VenueCall>,
    scheduled: Vec<VenueOutcome>,
}

impl ScriptedVenue {
    /// Construct an empty script.
    pub fn new(steps: Vec<ScriptedStep>) -> Self {
        Self {
            steps: steps.into(),
            calls: Vec::new(),
            scheduled: Vec::new(),
        }
    }

    /// Successfully matched calls in receipt order.
    pub fn calls(&self) -> &[VenueCall] {
        &self.calls
    }

    /// The number of expected calls not yet received.
    pub fn remaining_steps(&self) -> usize {
        self.steps.len()
    }

    /// Outcomes scheduled so far, in scheduling order.
    pub fn scheduled(&self) -> &[VenueOutcome] {
        &self.scheduled
    }

    /// Match a submission and schedule its scripted outcomes.
    pub fn submit(&mut self, order: &OrderRequest) -> Result<(), UnexpectedVenueCall> {
        let call = VenueCall::new(
            VenueOperation::Submit,
            Some(order.order_id().to_string()),
            None,
            None,
        )
        .map_err(message)?;
        self.execute(call)
    }

    /// Match a cancellation and schedule its scripted outcomes.
    pub fn cancel(&mut self, order_id: &str) -> Result<(), UnexpectedVenueCall> {
        let call = VenueCall::new(
            VenueOperation::Cancel,
            Some(order_id.to_string()),
            None,
            None,
        )
        .map_err(message)?;
        self.execute(call)
    }

    /// Match a replacement and schedule its scripted outcomes.
    pub fn replace(
        &mut self,
        order_id: &str,
        replacement: &OrderRequest,
    ) -> Result<(), UnexpectedVenueCall> {
        let call = VenueCall::new(
            VenueOperation::Replace,
            Some(order_id.to_string()),
            Some(replacement.order_id().to_string()),
            None,
        )
        .map_err(message)?;
        self.execute(call)
    }

    /// Match a snapshot request and schedule its position events.
    pub fn request_position_snapshot(
        &mut self,
        account_id: &str,
    ) -> Result<(), UnexpectedVenueCall> {
        let call = VenueCall::new(
            VenueOperation::PositionSnapshot,
            None,
            None,
            Some(account_id.to_string()),
        )
        .map_err(message)?;
        self.execute(call)
    }

    /// Assert that every scripted call has been consumed.
    pub fn assert_exhausted(&self) -> Result<(), UnexpectedVenueCall> {
        if let Some(next) = self.steps.front() {
            return Err(UnexpectedVenueCall {
                message: format!(
                    "{} scripted venue call(s) remain; next expected call is {:?}",
                    self.steps.len(),
                    next.call
                ),
            });
        }
        Ok(())
    }

    fn execute(&mut self, call: VenueCall) -> Result<(), UnexpectedVenueCall> {
        let Some(step) = self.steps.front() else {
            return Err(UnexpectedVenueCall {
                message: format!("unexpected venue call {call:?}; no scripted calls remain"),
            });
        };
        if call != step.call {
            return Err(UnexpectedVenueCall {
                message: format!("unexpected venue call {call:?}; expected {:?}", step.call),
            });
        }
        let step = self.steps.pop_front().expect("front checked");
        self.calls.push(call);
        self.scheduled.extend(step.outcomes);
        Ok(())
    }
}
