use super::*;

#[test]
fn a_stalled_reservation_cannot_extend_a_permit_lifetime() {
    let grant = BudgetGrant {
        count: 32,
        valid_for: std::time::Duration::from_secs(1),
        retry_after: std::time::Duration::ZERO,
    };
    let before_rpc = tokio::time::Instant::now() - std::time::Duration::from_secs(2);
    let mut expired = CachedGrant::received(grant, before_rpc);
    assert!(
        !expired.take(),
        "late acknowledgement cannot revive expired permits"
    );
    assert_eq!(
        expired.count, 32,
        "unused reservations remain charged at the authority"
    );

    let mut live = CachedGrant::received(grant, tokio::time::Instant::now());
    for _ in 0..32 {
        assert!(live.take());
    }
    assert!(!live.take());
}
