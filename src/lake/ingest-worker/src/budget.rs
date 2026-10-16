//! Short-lived fleet permits remain charged across lost acknowledgements and restarts.
use lake_ingest_jobs::{BudgetGrant, JobPersistence};
use lake_providers::{ProviderError, RequestBudget};
use std::sync::Arc;

pub struct SharedBudget<S> {
    store: S,
    scope: String,
    limit: u32,
    permits: tokio::sync::Mutex<PermitState>,
}
struct CachedGrant {
    count: u32,
    issued: u32,
    expires: tokio::time::Instant,
    grow_before: tokio::time::Instant,
}
impl CachedGrant {
    fn received(grant: BudgetGrant, started: tokio::time::Instant) -> Self {
        Self {
            count: grant.count,
            issued: grant.count,
            expires: started + grant.valid_for,
            grow_before: started + grant.valid_for / 2,
        }
    }
    fn take(&mut self, now: tokio::time::Instant) -> bool {
        if self.count == 0 || now >= self.expires {
            return false;
        }
        self.count -= 1;
        true
    }
}
struct PermitState {
    batch: u32,
    grant: Option<CachedGrant>,
}
impl PermitState {
    fn new() -> Self {
        Self {
            batch: 32,
            grant: None,
        }
    }
    fn request_size(&mut self, now: tokio::time::Instant, limit: u32) -> u32 {
        if let Some(previous) = self.grant.take() {
            if now >= previous.expires && previous.count > 0 {
                // Discarding an expired permit never refunds its authority charge.
                self.batch = previous.issued.saturating_sub(previous.count).max(1);
            } else if previous.count == 0 && now < previous.grow_before {
                // Grow only with substantial spare lifetime; near-expiry exhaustion
                // otherwise oscillates between an oversized and a useful batch.
                self.batch = self.batch.saturating_mul(2).min(32);
            }
        }
        self.batch.min(32).min(limit)
    }
}
impl<S: JobPersistence> SharedBudget<S> {
    pub fn new(store: S, endpoint: &str, key: &str, limit: u32) -> Arc<Self> {
        Arc::new(Self {
            store,
            scope: lake_ingest_jobs::budget_scope(endpoint, key),
            limit,
            permits: tokio::sync::Mutex::new(PermitState::new()),
        })
    }
}
impl<S: JobPersistence> RequestBudget for SharedBudget<S> {
    fn acquire(&self) -> lake_providers::BoxFuture<'_, Result<(), ProviderError>> {
        Box::pin(async move {
            let mut cached = self.permits.lock().await;
            loop {
                if let Some(grant) = cached.grant.as_mut() {
                    if grant.take(tokio::time::Instant::now()) {
                        return Ok(());
                    }
                }
                let started = tokio::time::Instant::now();
                let count = cached.request_size(started, self.limit);
                let grant = self
                    .store
                    .reserve_requests(&self.scope, self.limit, count)
                    .await
                    .map_err(|e| {
                        ProviderError::Configuration(format!(
                            "fleet request budget unavailable: {e}"
                        ))
                    })?;
                if grant.count == 0 {
                    tokio::time::sleep(grant.retry_after.max(std::time::Duration::from_millis(1)))
                        .await;
                } else {
                    cached.grant = Some(CachedGrant::received(grant, started));
                }
            }
        })
    }
}
#[cfg(test)]
#[path = "../tests/unit/budget.rs"]
mod tests;
