//! Short-lived fleet permits remain charged across lost acknowledgements and restarts.
use lake_ingest_jobs::{BudgetGrant, RequestAuthority};
use lake_providers::{ProviderError, RequestBudget};
use std::sync::Arc;

pub struct SharedBudget<S> {
    cache: Arc<Cache>,
    _store: std::marker::PhantomData<S>,
    refill_task: tokio::task::JoinHandle<()>,
}
struct Cache {
    state: std::sync::Mutex<PermitState>,
    refill: tokio::sync::Notify,
    changed: tokio::sync::Notify,
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
    grants: std::collections::VecDeque<CachedGrant>,
    error: Option<String>,
}
impl PermitState {
    fn new() -> Self {
        Self {
            batch: 32,
            grants: Default::default(),
            error: None,
        }
    }
    fn discard_expired(&mut self, now: tokio::time::Instant) {
        while self
            .grants
            .front()
            .is_some_and(|g| now >= g.expires || g.count == 0)
        {
            let previous = self.grants.pop_front().expect("front present");
            if now >= previous.expires && previous.count > 0 {
                self.batch = previous.issued.saturating_sub(previous.count).max(1);
            } else if previous.count == 0 && now < previous.grow_before {
                self.batch = self.batch.saturating_mul(2).min(128);
            }
        }
    }
    fn available(&self) -> u32 {
        self.grants.iter().map(|g| g.count).sum()
    }
}
impl<S: RequestAuthority> SharedBudget<S> {
    pub fn new(store: S, endpoint: &str, key: &str, limit: u32) -> Arc<Self> {
        let cache = Arc::new(Cache {
            state: std::sync::Mutex::new(PermitState::new()),
            refill: tokio::sync::Notify::new(),
            changed: tokio::sync::Notify::new(),
        });
        let worker = cache.clone();
        let scope = lake_ingest_jobs::budget_scope(endpoint, key);
        let refill_task = tokio::spawn(async move {
            loop {
                worker.refill.notified().await;
                let started = tokio::time::Instant::now();
                let count = {
                    let mut state = worker.state.lock().unwrap_or_else(|e| e.into_inner());
                    state.discard_expired(started);
                    if state.available() > 8 {
                        continue;
                    }
                    state.batch.min(limit)
                };
                let rpc = lake_pipeline::Pipeline::global().timer(lake_pipeline::Stage::RateRpc);
                let result = store.reserve_requests(&scope, limit, count).await;
                drop(rpc);
                match result {
                    Ok(grant) if grant.count > 0 => {
                        let mut state = worker.state.lock().unwrap_or_else(|e| e.into_inner());
                        state
                            .grants
                            .push_back(CachedGrant::received(grant, started));
                        state.discard_expired(tokio::time::Instant::now());
                    }
                    Ok(grant) => {
                        let capacity = lake_pipeline::Pipeline::global()
                            .timer(lake_pipeline::Stage::RateCapacity);
                        tokio::time::sleep(
                            grant.retry_after.max(std::time::Duration::from_millis(1)),
                        )
                        .await;
                        drop(capacity);
                        worker.refill.notify_one();
                    }
                    Err(error) => {
                        worker.state.lock().unwrap_or_else(|e| e.into_inner()).error =
                            Some(format!("fleet request budget unavailable: {error}"));
                    }
                }
                worker.changed.notify_waiters();
            }
        });
        Arc::new(Self {
            cache,
            _store: std::marker::PhantomData,
            refill_task,
        })
    }
}
impl<S> Drop for SharedBudget<S> {
    fn drop(&mut self) {
        self.refill_task.abort();
    }
}
impl<S: RequestAuthority> RequestBudget for SharedBudget<S> {
    fn acquire(&self) -> lake_providers::BoxFuture<'_, Result<(), ProviderError>> {
        Box::pin(async move {
            let _wait = lake_pipeline::Pipeline::global().timer(lake_pipeline::Stage::RateCache);
            loop {
                let changed = self.cache.changed.notified();
                tokio::pin!(changed);
                changed.as_mut().enable();
                let taken = {
                    let mut state = self.cache.state.lock().unwrap_or_else(|e| e.into_inner());
                    if let Some(error) = &state.error {
                        return Err(ProviderError::Configuration(error.clone()));
                    }
                    let now = tokio::time::Instant::now();
                    state.discard_expired(now);
                    let taken = state
                        .grants
                        .front_mut()
                        .is_some_and(|grant| grant.take(now));
                    if state.available() <= 8 {
                        self.cache.refill.notify_one();
                    }
                    taken
                };
                if taken {
                    return Ok(());
                }
                changed.await;
            }
        })
    }
}
#[cfg(test)]
#[path = "../tests/unit/budget.rs"]
mod tests;
