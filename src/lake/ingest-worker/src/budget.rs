//! Short-lived fleet permits remain charged across lost acknowledgements and restarts.
use lake_ingest_jobs::{BudgetGrant, JobPersistence};
use lake_providers::{ProviderError, RequestBudget};
use std::sync::Arc;

pub struct SharedBudget<S> {
    store: S,
    scope: String,
    limit: u32,
    grant: tokio::sync::Mutex<Option<CachedGrant>>,
}
struct CachedGrant {
    count: u32,
    expires: tokio::time::Instant,
}
impl CachedGrant {
    fn received(grant: BudgetGrant, started: tokio::time::Instant) -> Self {
        Self {
            count: grant.count,
            expires: started + grant.valid_for,
        }
    }
    fn take(&mut self) -> bool {
        if self.count == 0 || tokio::time::Instant::now() >= self.expires {
            return false;
        }
        self.count -= 1;
        true
    }
}
impl<S: JobPersistence> SharedBudget<S> {
    pub fn new(store: S, endpoint: &str, key: &str, limit: u32) -> Arc<Self> {
        Arc::new(Self {
            store,
            scope: lake_ingest_jobs::budget_scope(endpoint, key),
            limit,
            grant: tokio::sync::Mutex::new(None),
        })
    }
}
impl<S: JobPersistence> RequestBudget for SharedBudget<S> {
    fn acquire(&self) -> lake_providers::BoxFuture<'_, Result<(), ProviderError>> {
        Box::pin(async move {
            let mut cached = self.grant.lock().await;
            loop {
                if let Some(grant) = cached.as_mut() {
                    if grant.take() {
                        return Ok(());
                    }
                }
                let started = tokio::time::Instant::now();
                let grant = self
                    .store
                    .reserve_requests(&self.scope, self.limit, 32.min(self.limit))
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
                    *cached = Some(CachedGrant::received(grant, started));
                }
            }
        })
    }
}
#[cfg(test)]
#[path = "../tests/unit/budget.rs"]
mod tests;
