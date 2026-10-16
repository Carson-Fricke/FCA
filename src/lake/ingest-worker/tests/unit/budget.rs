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
        !expired.take(tokio::time::Instant::now()),
        "late acknowledgement cannot revive expired permits"
    );
    assert_eq!(
        expired.count, 32,
        "unused reservations remain charged at the authority"
    );

    let mut live = CachedGrant::received(grant, tokio::time::Instant::now());
    for _ in 0..32 {
        assert!(live.take(tokio::time::Instant::now()));
    }
    assert!(!live.take(tokio::time::Instant::now()));
}

fn grant(count: u32, started: tokio::time::Instant) -> CachedGrant {
    CachedGrant::received(
        BudgetGrant {
            count,
            valid_for: std::time::Duration::from_secs(1),
            retry_after: std::time::Duration::ZERO,
        },
        started,
    )
}

#[test]
fn expired_slow_or_idle_grants_shrink_without_refunding_or_boundary_oscillation() {
    let start = tokio::time::Instant::now();
    let mut state = PermitState::new();
    assert_eq!(state.request_size(start, 10000), 32);
    state.grant = Some(grant(32, start));
    for _ in 0..18 {
        assert!(state.grant.as_mut().unwrap().take(start));
    }
    assert_eq!(
        state.request_size(start + std::time::Duration::from_secs(1), 10000),
        18
    );
    state.grant = Some(grant(18, start));
    let near_expiry = start + std::time::Duration::from_millis(999);
    for _ in 0..18 {
        assert!(state.grant.as_mut().unwrap().take(near_expiry));
    }
    assert_eq!(
        state.request_size(near_expiry, 10000),
        18,
        "near-expiry exhaustion must not grow back to32"
    );
    state.grant = Some(grant(18, start));
    assert_eq!(
        state.request_size(start + std::time::Duration::from_secs(2), 10000),
        1,
        "idle reservations must not consume32 permits repeatedly"
    );
}

#[test]
fn early_exhaustion_grows_within_the_batch_and_configured_limits() {
    let start = tokio::time::Instant::now();
    let mut state = PermitState {
        batch: 1,
        grant: Some(grant(1, start)),
    };
    for expected in [2, 4, 8, 16, 32, 32] {
        let early = start + std::time::Duration::from_millis(100);
        let cached = state.grant.as_mut().unwrap();
        while cached.take(early) {}
        assert_eq!(state.request_size(early, 10000), expected);
        state.grant = Some(grant(expected, start));
    }
    assert_eq!(state.request_size(start, 5), 5);
}
