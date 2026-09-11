use super::*;

#[test]
fn resume_plan_restores_then_resumes() {
    let plan = plan_recovery(
        BootPolicyKind::ResumeReconcile,
        RecoveryFacts {
            has_snapshot: true,
            ..RecoveryFacts::default()
        },
    );
    assert_eq!(
        plan.steps
            .iter()
            .map(|step| step.action)
            .collect::<Vec<_>>(),
        vec![RecoveryAction::RestoreSnapshot, RecoveryAction::Resume]
    );
    assert_eq!(plan.terminal, RecoveryAction::Resume);
}

#[test]
fn divergence_halts_instead_of_resuming() {
    let plan = plan_recovery(
        BootPolicyKind::ResumeReconcile,
        RecoveryFacts {
            has_snapshot: true,
            diverged: true,
            ..RecoveryFacts::default()
        },
    );
    assert_eq!(plan.terminal, RecoveryAction::Halt);
}

#[test]
fn flatten_halt_cancels_and_flattens() {
    let plan = plan_recovery(
        BootPolicyKind::FlattenHalt,
        RecoveryFacts {
            has_resting_orders: true,
            has_open_positions: true,
            ..RecoveryFacts::default()
        },
    );
    assert_eq!(
        plan.steps
            .iter()
            .map(|step| step.action)
            .collect::<Vec<_>>(),
        vec![
            RecoveryAction::CancelOrders,
            RecoveryAction::FlattenPositions,
            RecoveryAction::Halt,
        ]
    );
}
