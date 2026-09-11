//! Startup recovery policy for the paper runtime.
//!
//! Deterministic core of `harness.runtime.recovery`: given a boot policy and
//! the preflight facts (a valid snapshot, a reconciliation divergence, and the
//! presence of open positions/orders), produce the ordered recovery steps and
//! terminal action. Broker cancellation and flattening stay behind ports in
//! [`super::paper`].

/// Boot policy selected by configuration.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BootPolicyKind {
    /// Resume from the snapshot after reconciling.
    ResumeReconcile,
    /// Flatten positions and halt.
    FlattenHalt,
    /// Halt without touching the broker.
    HaltOnly,
}

impl BootPolicyKind {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::ResumeReconcile => "resume_reconcile",
            Self::FlattenHalt => "flatten_halt",
            Self::HaltOnly => "halt_only",
        }
    }
}

/// One terminal or intermediate recovery action.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RecoveryAction {
    /// Restore the snapshot.
    RestoreSnapshot,
    /// Cancel resting orders.
    CancelOrders,
    /// Flatten open positions.
    FlattenPositions,
    /// Resume normal operation.
    Resume,
    /// Halt the runtime.
    Halt,
}

impl RecoveryAction {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::RestoreSnapshot => "restore_snapshot",
            Self::CancelOrders => "cancel_orders",
            Self::FlattenPositions => "flatten_positions",
            Self::Resume => "resume",
            Self::Halt => "halt",
        }
    }
}

/// The facts a recovery decision is computed from.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub struct RecoveryFacts {
    /// Whether a valid snapshot is available.
    pub has_snapshot: bool,
    /// Whether reconciliation detected a divergence.
    pub diverged: bool,
    /// Whether the broker reports open positions.
    pub has_open_positions: bool,
    /// Whether the broker reports resting orders.
    pub has_resting_orders: bool,
}

/// One ordered recovery step.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RecoveryStep {
    /// The action to perform.
    pub action: RecoveryAction,
    /// Why the action is required.
    pub reason: &'static str,
}

/// The computed recovery plan.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RecoveryPlan {
    /// Ordered steps.
    pub steps: Vec<RecoveryStep>,
    /// Terminal action.
    pub terminal: RecoveryAction,
}

/// Build the deterministic recovery plan for one boot.
#[must_use]
pub fn plan_recovery(policy: BootPolicyKind, facts: RecoveryFacts) -> RecoveryPlan {
    let mut steps = Vec::new();
    if facts.has_snapshot {
        steps.push(RecoveryStep {
            action: RecoveryAction::RestoreSnapshot,
            reason: "restore the last valid snapshot",
        });
    }
    match policy {
        BootPolicyKind::ResumeReconcile => {
            if facts.has_resting_orders {
                steps.push(RecoveryStep {
                    action: RecoveryAction::CancelOrders,
                    reason: "cancel resting orders before resuming",
                });
            }
            if facts.diverged {
                steps.push(RecoveryStep {
                    action: RecoveryAction::Halt,
                    reason: "reconciliation diverged; refusing to resume",
                });
                RecoveryPlan {
                    steps,
                    terminal: RecoveryAction::Halt,
                }
            } else {
                steps.push(RecoveryStep {
                    action: RecoveryAction::Resume,
                    reason: "state matches the broker",
                });
                RecoveryPlan {
                    steps,
                    terminal: RecoveryAction::Resume,
                }
            }
        }
        BootPolicyKind::FlattenHalt => {
            if facts.has_resting_orders {
                steps.push(RecoveryStep {
                    action: RecoveryAction::CancelOrders,
                    reason: "cancel resting orders before flattening",
                });
            }
            if facts.has_open_positions {
                steps.push(RecoveryStep {
                    action: RecoveryAction::FlattenPositions,
                    reason: "flatten open positions",
                });
            }
            steps.push(RecoveryStep {
                action: RecoveryAction::Halt,
                reason: "flatten-halt boot policy",
            });
            RecoveryPlan {
                steps,
                terminal: RecoveryAction::Halt,
            }
        }
        BootPolicyKind::HaltOnly => {
            steps.push(RecoveryStep {
                action: RecoveryAction::Halt,
                reason: "halt-only boot policy",
            });
            RecoveryPlan {
                steps,
                terminal: RecoveryAction::Halt,
            }
        }
    }
}

#[cfg(test)]
#[path = "../../tests/unit/runtime_recovery.rs"]
mod tests;
