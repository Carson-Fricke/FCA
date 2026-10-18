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
fn expired_unused_permits_shrink_and_early_exhaustion_grows_without_refunds() {
    let start = tokio::time::Instant::now();
    let mut state = PermitState::new();
    state.grants.push_back(grant(32, start));
    for _ in 0..18 {
        assert!(state.grants.front_mut().unwrap().take(start));
    }
    state.discard_expired(start + std::time::Duration::from_secs(1));
    assert_eq!(state.batch, 18);
    assert_eq!(state.available(), 0);
    state.grants.push_back(grant(18, start));
    while state.grants.front_mut().unwrap().take(start) {}
    state.discard_expired(start);
    assert_eq!(state.batch, 36);
    state.grants.push_back(grant(36, start));
    state.discard_expired(start + std::time::Duration::from_secs(2));
    assert_eq!(state.batch, 1);
}

#[derive(Clone)]
struct Authority {
    calls: Arc<std::sync::atomic::AtomicUsize>,
    stalled: Arc<tokio::sync::Notify>,
    release: Arc<tokio::sync::Notify>,
}
#[async_trait::async_trait]
impl RequestAuthority for Authority {
    async fn reserve_requests(
        &self,
        _: &str,
        _: u32,
        _: u32,
    ) -> Result<BudgetGrant, lake_contracts::DataError> {
        if self.calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst) > 0 {
            self.stalled.notify_one();
            self.release.notified().await;
        }
        Ok(BudgetGrant {
            count: 10,
            valid_for: std::time::Duration::from_secs(5),
            retry_after: std::time::Duration::ZERO,
        })
    }
}
#[tokio::test]
async fn cached_permits_are_available_while_the_authority_refill_is_stalled() {
    let authority = Authority {
        calls: Arc::default(),
        stalled: Arc::default(),
        release: Arc::default(),
    };
    let budget = SharedBudget::new(authority.clone(), "fake", "key", 10000);
    budget.acquire().await.unwrap();
    budget.acquire().await.unwrap();
    tokio::time::timeout(
        std::time::Duration::from_secs(1),
        authority.stalled.notified(),
    )
    .await
    .unwrap();
    for _ in 0..8 {
        tokio::time::timeout(std::time::Duration::from_millis(250), budget.acquire())
            .await
            .expect("cached permits cannot wait on storage I/O")
            .unwrap();
    }
    let next = budget.acquire();
    tokio::pin!(next);
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(5), &mut next)
            .await
            .is_err()
    );
    authority.release.notify_one();
    next.await.unwrap();
}
