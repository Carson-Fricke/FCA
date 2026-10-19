//! Execution and durable ownership ports shared by local processes and Azure containers.
use crate::{DurableJob, ExecutionState, WorkRef, WorkerSpec};
use lake_contracts::DataError;

#[async_trait::async_trait]
pub trait WorkerRunner: Clone + Send + Sync + 'static {
    fn begin_reconcile(&self);
    fn configuration(&self) -> WorkerSpec;
    fn for_work(&self, specification: &WorkerSpec) -> Result<Self, DataError>;
    async fn inspect(&self, work: &WorkRef) -> Result<ExecutionState, DataError>;
    async fn start(&self, work: &WorkRef) -> Result<(), DataError>;
    async fn delete(&self, work: &WorkRef) -> Result<(), DataError>;
}

#[async_trait::async_trait]
pub trait JobOwnership: Send + Sync {
    async fn renew(&self) -> Result<(), DataError>;
    async fn release(&self) -> Result<(), DataError>;
    async fn save(&self, job: &DurableJob) -> Result<(), DataError>;
}

#[derive(Clone)]
pub struct ActiveJob {
    pub id: String,
    pub dispatched: bool,
    pub created: i64,
}

#[async_trait::async_trait]
pub trait JobPersistence: crate::RequestAuthority {
    type Owner: JobOwnership + 'static;
    async fn admit(&self, job: DurableJob) -> Result<DurableJob, DataError>;
    async fn get(&self, id: &str) -> Result<DurableJob, DataError>;
    async fn admit_work(&self, work: DurableJob) -> Result<DurableJob, DataError>;
    async fn get_work(&self, work: &WorkRef) -> Result<DurableJob, DataError>;
    async fn own_work(&self, work: &WorkRef) -> Result<Self::Owner, DataError>;
    async fn own(&self, id: &str) -> Result<Self::Owner, DataError>;
    async fn dispatch_lock(&self) -> Result<Self::Owner, DataError>;
    async fn active_jobs(&self) -> Result<Vec<ActiveJob>, DataError>;
    /// Unstarted immutable work addresses; implementations may use an atomic
    /// metadata listing to avoid reading an entire multi-year plan per claim.
    async fn pending_work(&self) -> Result<Vec<WorkRef>, DataError> {
        let mut pending = Vec::new();
        for root in self.active_jobs().await? {
            for part in self.get(&root.id).await?.partitions()? {
                let work = self.get_work(&part.reference()).await?;
                if !work.dispatch_started && !work.terminal() {
                    pending.push(work.reference());
                }
            }
        }
        Ok(pending)
    }
}

pub async fn while_owned<T>(
    owner: &impl JobOwnership,
    work: impl std::future::Future<Output = Result<T, DataError>>,
) -> Result<T, DataError> {
    tokio::pin!(work);
    let mut heartbeat = tokio::time::interval_at(
        tokio::time::Instant::now() + std::time::Duration::from_secs(15),
        std::time::Duration::from_secs(15),
    );
    loop {
        tokio::select! {
            biased;
            _ = heartbeat.tick() => owner.renew().await?,
            result = &mut work => return result,
        }
    }
}

/// Rolling provider allowance authority, independent of execution lifecycle storage.
#[async_trait::async_trait]
pub trait RequestAuthority: Clone + Send + Sync + 'static {
    async fn reserve_requests(
        &self,
        scope: &str,
        limit: u32,
        count: u32,
    ) -> Result<crate::BudgetGrant, DataError>;
}
