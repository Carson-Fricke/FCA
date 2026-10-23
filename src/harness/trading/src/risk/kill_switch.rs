//! Explicit, audited global kill switch for the pre-trade risk pipelines.
//!
//! The kill switch is one operator-owned [`RiskRule`] that, while engaged,
//! rejects every candidate order with an explicit, audited decision and marks
//! the rejection as a halt. Engagement and disengagement are explicit operator
//! calls recorded in an immutable audit trail; the switch has no implicit
//! timeouts, no wall clock, no randomness, and no I/O.

use crate::error::require_text;
use crate::error::LiveResult;
use crate::orders::OrderRequest;
use crate::risk::models::RiskContext;
use crate::risk::models::RiskRule;
use crate::risk::models::RuleDecision;

/// The default kill-switch rule identifier.
pub const DEFAULT_KILL_SWITCH_RULE_ID: &str = "global-kill-switch";

/// The two explicit states of the global kill switch.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum KillSwitchState {
    /// Rejecting every order.
    Engaged,
    /// Allowing every order.
    Disengaged,
}

impl KillSwitchState {
    /// The canonical wire spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Engaged => "engaged",
            Self::Disengaged => "disengaged",
        }
    }
}

/// The audited control actions recorded by the switch.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum KillSwitchAction {
    /// Engage the switch.
    Trip,
    /// Disengage the switch.
    Release,
}

/// One immutable, audited kill-switch control action.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KillSwitchEvent {
    /// One-based ordinal in the owning switch's audit trail.
    pub sequence: usize,
    /// The control action.
    pub action: KillSwitchAction,
    /// Optional provenance reason.
    pub reason: Option<String>,
    /// Optional provenance actor.
    pub actor: Option<String>,
}

/// Global kill switch rule: reject every order while engaged.
#[derive(Clone, Debug)]
pub struct KillSwitch {
    /// The stable rule identifier.
    pub rule_id: String,
    state: KillSwitchState,
    events: Vec<KillSwitchEvent>,
}

impl KillSwitch {
    /// Construct a disengaged kill switch.
    pub fn new(rule_id: Option<&str>) -> LiveResult<Self> {
        Ok(Self {
            rule_id: require_text(rule_id.unwrap_or(DEFAULT_KILL_SWITCH_RULE_ID), "rule_id")?,
            state: KillSwitchState::Disengaged,
            events: Vec::new(),
        })
    }

    /// The current explicit switch state.
    pub fn state(&self) -> KillSwitchState {
        self.state
    }

    /// Whether the switch currently rejects every order.
    pub fn engaged(&self) -> bool {
        self.state == KillSwitchState::Engaged
    }

    /// The complete ordered audit trail of control actions.
    pub fn events(&self) -> &[KillSwitchEvent] {
        &self.events
    }

    /// The most recent control action, if any.
    pub fn last_event(&self) -> Option<&KillSwitchEvent> {
        self.events.last()
    }

    /// Engage the switch and record one audited trip action.
    pub fn trip(
        &mut self,
        reason: Option<&str>,
        actor: Option<&str>,
    ) -> LiveResult<KillSwitchEvent> {
        self.record(
            KillSwitchAction::Trip,
            KillSwitchState::Engaged,
            reason,
            actor,
        )
    }

    /// Disengage the switch and record one audited release action.
    pub fn release(
        &mut self,
        reason: Option<&str>,
        actor: Option<&str>,
    ) -> LiveResult<KillSwitchEvent> {
        self.record(
            KillSwitchAction::Release,
            KillSwitchState::Disengaged,
            reason,
            actor,
        )
    }

    fn record(
        &mut self,
        action: KillSwitchAction,
        state: KillSwitchState,
        reason: Option<&str>,
        actor: Option<&str>,
    ) -> LiveResult<KillSwitchEvent> {
        let reason = match reason {
            Some(value) => Some(require_text(value, "reason")?),
            None => None,
        };
        let actor = match actor {
            Some(value) => Some(require_text(value, "actor")?),
            None => None,
        };
        let event = KillSwitchEvent {
            sequence: self.events.len() + 1,
            action,
            reason,
            actor,
        };
        self.state = state;
        self.events.push(event.clone());
        Ok(event)
    }

    fn metrics(&self) -> Vec<(&'static str, String)> {
        let mut metrics = vec![
            ("switch", self.rule_id.clone()),
            ("state", self.state.as_str().to_string()),
        ];
        if let Some(last) = self.last_event() {
            if last.action == KillSwitchAction::Trip {
                metrics.push(("trip_sequence", last.sequence.to_string()));
                if let Some(reason) = &last.reason {
                    metrics.push(("trip_reason", reason.clone()));
                }
                if let Some(actor) = &last.actor {
                    metrics.push(("trip_actor", actor.clone()));
                }
            }
        }
        metrics
    }
}

impl RiskRule for KillSwitch {
    fn rule_id(&self) -> &str {
        &self.rule_id
    }

    fn evaluate(&self, order: &OrderRequest, _context: &RiskContext) -> LiveResult<RuleDecision> {
        let decision = if self.state == KillSwitchState::Engaged {
            RuleDecision::reject(order.clone(), &format!("{} engaged", self.rule_id), true)
        } else {
            RuleDecision::allow(order.clone())
        };
        let mut decision = decision;
        for (key, value) in self.metrics() {
            decision = decision.with_metric(key, value);
        }
        Ok(decision)
    }
}
