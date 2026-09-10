//! The paper-to-live promotion gate.
//!
//! Deterministic core of `harness.runtime.go_live`: evaluate one paper session
//! of evidence against the promotion criteria (minimum sessions, clean streak,
//! reconciliation match, shadow agreement) and produce a [`GateDecision`].
//! The live deployment validator and broker wiring stay behind ports.

/// One go-live evidence session.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PaperSessionEvidence {
    /// Session identifier.
    pub session_id: String,
    /// Whether reconciliation matched at session end.
    pub reconciled: bool,
    /// Whether the shadow comparison agreed.
    pub shadow_agreed: bool,
    /// Whether the session completed without a kill switch.
    pub completed: bool,
}

impl PaperSessionEvidence {
    /// Whether the session is clean enough to count toward the streak.
    #[must_use]
    pub fn is_clean(&self) -> bool {
        self.reconciled && self.shadow_agreed && self.completed
    }
}

/// Gate state for one promotion decision.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GateState {
    /// Promotion is allowed.
    Promoted,
    /// More clean sessions are required.
    Pending,
    /// A criterion failed and promotion is refused.
    Refused,
}

/// The gate's verdict for one evidence set.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GateDecision {
    /// Verdict.
    pub state: GateState,
    /// Clean sessions in the current streak.
    pub clean_streak: usize,
    /// Minimum required clean streak.
    pub required_streak: usize,
    /// Failure reason, when refused.
    pub reason: Option<String>,
}

/// Promotion criteria.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct GateCriteria {
    /// Minimum number of clean sessions required.
    pub required_streak: usize,
}

impl Default for GateCriteria {
    fn default() -> Self {
        Self {
            required_streak: 10,
        }
    }
}

/// Return the length of the trailing clean streak.
#[must_use]
pub fn clean_streak(sessions: &[PaperSessionEvidence]) -> usize {
    let mut streak = 0usize;
    for session in sessions.iter().rev() {
        if session.is_clean() {
            streak += 1;
        } else {
            break;
        }
    }
    streak
}

/// Evaluate one evidence set against the promotion criteria.
#[must_use]
pub fn evaluate(sessions: &[PaperSessionEvidence], criteria: GateCriteria) -> GateDecision {
    let streak = clean_streak(sessions);
    if sessions.iter().rev().any(|session| !session.completed) {
        return GateDecision {
            state: GateState::Refused,
            clean_streak: streak,
            required_streak: criteria.required_streak,
            reason: Some("a paper session did not complete".to_string()),
        };
    }
    if sessions.iter().rev().any(|session| !session.reconciled) {
        return GateDecision {
            state: GateState::Refused,
            clean_streak: streak,
            required_streak: criteria.required_streak,
            reason: Some("a paper session failed reconciliation".to_string()),
        };
    }
    if streak >= criteria.required_streak {
        GateDecision {
            state: GateState::Promoted,
            clean_streak: streak,
            required_streak: criteria.required_streak,
            reason: None,
        }
    } else {
        GateDecision {
            state: GateState::Pending,
            clean_streak: streak,
            required_streak: criteria.required_streak,
            reason: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn session(id: u32, clean: bool) -> PaperSessionEvidence {
        PaperSessionEvidence {
            session_id: format!("s{id}"),
            reconciled: clean,
            shadow_agreed: clean,
            completed: true,
        }
    }

    #[test]
    fn pending_until_the_streak_is_reached() {
        let sessions: Vec<PaperSessionEvidence> = (0..3).map(|id| session(id, true)).collect();
        let decision = evaluate(
            &sessions,
            GateCriteria {
                required_streak: 5,
            },
        );
        assert_eq!(decision.state, GateState::Pending);
        assert_eq!(decision.clean_streak, 3);
    }

    #[test]
    fn promotes_on_a_full_clean_streak() {
        let sessions: Vec<PaperSessionEvidence> = (0..5).map(|id| session(id, true)).collect();
        let decision = evaluate(
            &sessions,
            GateCriteria {
                required_streak: 5,
            },
        );
        assert_eq!(decision.state, GateState::Promoted);
    }

    #[test]
    fn refuses_on_reconciliation_failure() {
        let sessions = vec![session(1, true), session(2, false)];
        let decision = evaluate(&sessions, GateCriteria::default());
        assert_eq!(decision.state, GateState::Refused);
        assert_eq!(decision.clean_streak, 0);
    }
}
