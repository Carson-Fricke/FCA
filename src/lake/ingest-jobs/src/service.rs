//! One durable execution, disjoint leased partitions and a globally bounded worker fleet.
use crate::{
    AcaRunner, DurableJob, ExecutionState, JobOwnership, JobPersistence, JobStore, WorkRef,
    WorkRequest, WorkerRunner,
};
use futures_util::{stream, StreamExt, TryStreamExt};
use lake_catalog::JobState;
use lake_contracts::DataError;
use std::sync::Arc;

#[derive(Clone)]
pub struct IngestionJobs<R = AcaRunner, S = JobStore> {
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
        result.and(lock.release().await)
    }
    async fn reconcile_owned(&self, lock: &S::Owner) -> Result<(), DataError> {
        self.runner.begin_reconcile();
        let roots = self.store.active_jobs().await?;
        let mut partitions = Vec::new();
        let mut jobs = Vec::new();
        let mut limit = self.runner.configuration().max_workers();
        for entry in roots {
            lock.renew().await?;
            let job = self.store.get(&entry.id).await?;
            limit = limit.min(job.execution.max_workers());
            // Read existing immutable work directly; conditional admission is only needed once.
            // Bounded storage fan-out prevents a multi-year manifest from serializing dispatch.
            let store = self.store.clone();
            let work = stream::iter(job.partitions()?)
                .map(move |partition| {
                    let store = store.clone();
                    async move {
                        match store.get_work(&partition.reference()).await {
                            Ok(existing) => Ok(existing),
                            Err(DataError::NotFound(_)) => store.admit_work(partition).await,
                            Err(error) => Err(error),
                        }
                    }
                })
                .buffer_unordered(16)
                .try_collect::<Vec<_>>()
                .await?;
            partitions.extend(work);
            jobs.push(job);
        }
        let mut active = 0usize;
        let mut pending = Vec::new();
        for work in &mut partitions {
            if work.execution_deleted {
                continue;
            }
            if !work.dispatch_started {
                pending.push(work.reference());
                continue;
            }
            lock.renew().await?;
            let reference = work.reference();
            let runner = self.runner.for_work(&work.execution)?;
            let state = runner.inspect(&reference).await?;
            if work.terminal() {
                runner.delete(&reference).await?;
                if state == ExecutionState::Active
                    && runner.inspect(&reference).await? == ExecutionState::Active
                {
                    active += 1;
                    continue;
                }
                self.mark_cleaned(&reference).await?;
                work.execution_deleted = true;
                continue;
            }
            active += 1;
            let expired = crate::model::now().micros()
                - work
                    .dispatched_at
                    .ok_or_else(|| DataError::value("partition dispatch timestamp is missing"))?
                > work.execution.timeout_seconds() as i64 * 1_000_000;
            match state {
                ExecutionState::Active if expired => { runner.delete(&reference).await?; }
                ExecutionState::Stopped => self.fail(&reference, "ingestion worker stopped before durable completion; inspect its execution logs").await?,
                ExecutionState::Absent if expired || work.record.state == JobState::Running => self.fail(&reference, "ingestion dispatch outcome is unknown or its worker disappeared; automatic replay is prohibited").await?,
                // A missing execution after POST is an unknown outcome, never permission to POST again.
                _ => {}
            }
        }
        // Interleave executions instead of letting one multi-year plan monopolize every slot.
        pending.sort_by(|a, b| a.partition.cmp(&b.partition).then(a.job_id.cmp(&b.job_id)));
        let starts = stream::iter(pending.into_iter().take(limit.saturating_sub(active)))
            .map(|reference| async move {
                lock.renew().await?;
                let owner = match self.store.own_work(&reference).await {
                    Ok(owner) => owner,
                    Err(DataError::Conflict(_)) => return Ok(()),
                    Err(error) => return Err(error),
                };
                let mut work = self.store.get_work(&reference).await?;
                if work.dispatch_started || work.terminal() {
                    owner.release().await?;
                    return Ok(());
                }
                work.dispatch_started = true;
                work.dispatched_at = Some(crate::model::now().micros());
                work.record.updated_at = crate::model::now();
                owner.save(&work).await?;
                owner.release().await?;
                self.runner
                    .for_work(&work.execution)?
                    .start(&reference)
                    .await
            })
            .buffer_unordered(limit.max(1))
            .try_collect::<Vec<_>>()
            .await;
        starts?;
        // Re-read durable state: workers may have already advanced during concurrent starts.
        for partition in &mut partitions {
            *partition = self.store.get_work(&partition.reference()).await?;
        }
        for job in jobs {
            self.aggregate(&job.record.job_id, &partitions).await?;
        }
        Ok(())
    }
    async fn mark_cleaned(&self, reference: &WorkRef) -> Result<(), DataError> {
        let owner = match self.store.own_work(reference).await {
            Ok(owner) => owner,
            Err(DataError::Conflict(_)) => return Ok(()),
            Err(e) => return Err(e),
        };
        let mut work = self.store.get_work(reference).await?;
        work.execution_deleted = true;
        owner.save(&work).await?;
        owner.release().await
    }
    async fn fail(&self, reference: &WorkRef, error: &str) -> Result<(), DataError> {
        let owner = match self.store.own_work(reference).await {
            Ok(owner) => owner,
            Err(DataError::Conflict(_)) => return Ok(()),
            Err(e) => return Err(e),
        };
        let mut work = self.store.get_work(reference).await?;
        if !work.terminal() {
            work.record.state = JobState::Failed;
            work.record.error_message = Some(error.into());
            work.record.updated_at = crate::model::now();
            owner.save(&work).await?;
        }
        owner.release().await
    }
    async fn aggregate(&self, id: &str, partitions: &[DurableJob]) -> Result<(), DataError> {
        let owner = match self.store.own(id).await {
            Ok(owner) => owner,
            Err(DataError::Conflict(_)) => return Ok(()),
            Err(e) => return Err(e),
        };
        let result = crate::while_owned(&owner, async {
            let mut job = self.store.get(id).await?;
            // Completed/pending history cannot change independently. Refresh only dispatched live work.
            let refresh: Vec<_> = partitions
                .iter()
                .filter(|work| work.record.job_id == id)
                .cloned()
                .collect();
            let store = self.store.clone();
            let works = stream::iter(refresh)
                .map(move |work| {
                    let store = store.clone();
                    async move {
                        if work.dispatch_started && !work.terminal() {
                            store.get_work(&work.reference()).await
                        } else {
                            Ok(work)
                        }
                    }
                })
                .buffer_unordered(16)
                .try_collect::<Vec<_>>()
                .await?;
            if !job.terminal() {
                let all_terminal = works.iter().all(DurableJob::terminal);
                let any_failed = works
                    .iter()
                    .any(|work| work.record.state == JobState::Failed);
                job.record.state = if all_terminal {
                    if any_failed {
                        JobState::Failed
                    } else {
                        JobState::Complete
                    }
                } else if works.iter().any(|work| work.dispatch_started) {
                    JobState::Running
                } else {
                    JobState::Queued
                };
                job.dispatch_started = works.iter().any(|work| work.dispatch_started);
                if job.dispatched_at.is_none() {
                    job.dispatched_at = works.iter().filter_map(|work| work.dispatched_at).min();
                }
                job.record.error_message = works
                    .iter()
                    .find_map(|work| work.record.error_message.clone());
                if let Some(latest) = works
                    .iter()
                    .filter(|work| work.record.progress.is_some())
                    .max_by_key(|work| work.record.updated_at)
                {
                    let mut progress = latest.record.progress.clone().expect("filtered progress");
                    progress.sessions_total = works
                        .iter()
                        .filter_map(|work| work.record.progress.as_ref())
                        .map(|p| p.sessions_total)
                        .sum();
                    progress.sessions_done = works
                        .iter()
                        .filter_map(|work| work.record.progress.as_ref())
                        .map(|p| p.sessions_done)
                        .sum();
                    job.record.progress = Some(progress);
                }
                job.result = works.iter().find_map(|work| work.result.clone());
                job.record.updated_at = crate::model::now();
            }
            job.execution_deleted = works.iter().all(|work| work.execution_deleted);
            owner.save(&job).await
        })
        .await;
        result.and(owner.release().await)
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
