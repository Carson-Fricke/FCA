//! Admission survives callers; a leased reconciler bounds aggregate ACI execution.
use crate::{
    AciRunner, DurableJob, GroupState, JobOwnership, JobPersistence, JobStore, WorkRequest,
    WorkerRunner,
};
use lake_catalog::JobState;
use lake_contracts::DataError;
use std::sync::Arc;

#[derive(Clone)]
pub struct IngestionJobs<R = AciRunner, S = JobStore> {
    pub store: S,
    pub runner: R,
}
impl<R: WorkerRunner, S: JobPersistence> IngestionJobs<R, S> {
    pub async fn submit(
        &self,
        id: String,
        requests: Vec<WorkRequest>,
    ) -> Result<DurableJob, DataError> {
        self.store
            .admit(DurableJob::new(id, requests, self.runner.configuration())?)
            .await
    }
    pub async fn reconcile(&self) -> Result<(), DataError> {
        let lock = match self.store.dispatch_lock().await {
            Ok(lock) => lock,
            Err(DataError::Conflict(_)) => return Ok(()),
            Err(e) => return Err(e),
        };
        let result = crate::while_owned(&lock, self.reconcile_owned(&lock)).await;
        let released = lock.release().await;
        result.and(released)
    }
    async fn reconcile_owned(&self, lock: &S::Owner) -> Result<(), DataError> {
        let records = self.store.active_jobs().await?;
        let mut pending = Vec::new();
        let mut active = 0;
        let mut limit = self.runner.configuration().max_workers();
        for entry in records {
            if !entry.dispatched {
                pending.push(entry.id);
                continue;
            }
            lock.renew().await?;
            let job = self.store.get(&entry.id).await?;
            let record = job.record.clone();
            let runner = self.runner.for_work(&job.execution)?;
            if matches!(record.state, JobState::Complete | JobState::Failed) {
                if job.group_deleted {
                    continue;
                }
                // Terminal groups must be stopped/deleted even when settlement preceded process exit.
                if runner.inspect(&record.job_id).await? != GroupState::Absent {
                    runner.delete(&record.job_id).await?;
                    if runner.inspect(&record.job_id).await? != GroupState::Absent {
                        active += 1;
                        limit = limit.min(job.execution.max_workers());
                        continue;
                    }
                }
                let owner = match self.store.own(&record.job_id).await {
                    Ok(owner) => owner,
                    Err(DataError::Conflict(_)) => continue,
                    Err(e) => return Err(e),
                };
                let mut job = self.store.get(&record.job_id).await?;
                job.group_deleted = true;
                owner.save(&job).await?;
                owner.release().await?;
                continue;
            }
            if !job.dispatch_started {
                pending.push(job.record.job_id);
                continue;
            }
            active += 1;
            limit = limit.min(job.execution.max_workers());
            match runner.inspect(&record.job_id).await? {
                GroupState::Absent => {
                    if crate::model::now().micros()
                        - job
                            .dispatched_at
                            .ok_or_else(|| DataError::value("dispatch timestamp is missing"))?
                        > job.execution.timeout_seconds() as i64 * 1_000_000
                    {
                        self.fail_stopped(
                            &record.job_id,
                            "ingestion deadline expired before worker could start",
                        )
                        .await?;
                    } else if record.state == JobState::Running {
                        self.fail_stopped(
                            &record.job_id,
                            "running ingestion worker disappeared; automatic replay is prohibited",
                        )
                        .await?;
                    } else {
                        lock.renew().await?;
                        runner.start(&record.job_id).await?;
                    }
                }
                GroupState::Active => {
                    if crate::model::now().micros()
                        - job
                            .dispatched_at
                            .ok_or_else(|| DataError::value("dispatch timestamp is missing"))?
                        > job.execution.timeout_seconds() as i64 * 1_000_000
                    {
                        runner.delete(&record.job_id).await?;
                        // DELETE is asynchronous. Retain the occupied slot until Azure confirms the group stopped.
                    }
                }
                GroupState::Stopped => {
                    self.fail_stopped(&record.job_id,"ingestion worker stopped before durable completion; inspect the retained job and container logs").await?;
                }
            }
        }
        // Drain older concurrency budgets before expanding a pool; mixed deployments cannot multiply provider limits.
        for id in pending {
            if active >= limit {
                break;
            }
            lock.renew().await?;
            let owner = match self.store.own(&id).await {
                Ok(owner) => owner,
                Err(DataError::Conflict(_)) => continue,
                Err(e) => return Err(e),
            };
            let mut job = self.store.get(&id).await?;
            limit = limit.min(job.execution.max_workers());
            if active >= limit {
                owner.release().await?;
                break;
            }
            if job.dispatch_started || job.terminal() {
                owner.release().await?;
                continue;
            }
            job.dispatch_started = true;
            job.record.updated_at = crate::model::now();
            job.dispatched_at = Some(job.record.updated_at.micros());
            owner.save(&job).await?;
            owner.release().await?;
            // If this call is interrupted, reconciliation inspects this exact resource before another PUT.
            self.runner
                .for_work(&job.execution)?
                .start(&job.record.job_id)
                .await?;
            active += 1;
        }
        Ok(())
    }
    async fn fail_stopped(&self, id: &str, message: &str) -> Result<(), DataError> {
        let owner = match self.store.own(id).await {
            Ok(owner) => owner,
            Err(DataError::Conflict(_)) => return Ok(()),
            Err(e) => return Err(e),
        };
        let mut job = self.store.get(id).await?;
        if !job.terminal() {
            job.record.state = JobState::Failed;
            job.record.error_message = Some(message.into());
            job.record.updated_at = crate::model::now();
            owner.save(&job).await?;
        }
        owner.release().await
    }
    pub fn start_reconciler(
        self: Arc<Self>,
        interval: std::time::Duration,
    ) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            loop {
                if let Err(error) = self.reconcile().await {
                    eprintln!("ingestion reconciliation failed: {error}");
                }
                tokio::time::sleep(interval).await;
            }
        })
    }
}
