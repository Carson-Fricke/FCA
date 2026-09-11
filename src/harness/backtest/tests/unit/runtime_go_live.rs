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
    let decision = evaluate(&sessions, GateCriteria { required_streak: 5 });
    assert_eq!(decision.state, GateState::Pending);
    assert_eq!(decision.clean_streak, 3);
}

#[test]
fn promotes_on_a_full_clean_streak() {
    let sessions: Vec<PaperSessionEvidence> = (0..5).map(|id| session(id, true)).collect();
    let decision = evaluate(&sessions, GateCriteria { required_streak: 5 });
    assert_eq!(decision.state, GateState::Promoted);
}

#[test]
fn refuses_on_reconciliation_failure() {
    let sessions = vec![session(1, true), session(2, false)];
    let decision = evaluate(&sessions, GateCriteria::default());
    assert_eq!(decision.state, GateState::Refused);
    assert_eq!(decision.clean_streak, 0);
}
