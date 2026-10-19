//! Reuse an admitted finite executor for unstarted immutable chunks only.
use crate::{DurableJob, JobOwnership, JobPersistence, WorkRef};
use lake_contracts::DataError;

/// The dispatcher lease serializes slot starts and worker reuse. The anchor
/// remains running until its process drains the pool, preventing early cleanup.
pub async fn claim_next<S: JobPersistence>(
    store: &S,
    anchor: &DurableJob,
    last_job: &str,
) -> Result<Option<WorkRef>, DataError> {
    let lock = loop {
        match store.dispatch_lock().await {
            Ok(lock) => break lock,
            Err(DataError::Conflict(_)) => {
                tokio::time::sleep(std::time::Duration::from_millis(100)).await
            }
            Err(e) => return Err(e),
        }
    };
    let result = crate::while_owned(&lock, async {
        let mut candidates = store.pending_work().await?;
        candidates.sort_by_key(|r| {
            (
                r.job_id.as_str() <= last_job,
                r.job_id.clone(),
                r.partition.clone(),
            )
        });
        for reference in candidates {
            lock.renew().await?;
            let owner = match store.own_work(&reference).await {
                Ok(owner) => owner,
                Err(DataError::Conflict(_)) => continue,
                Err(e) => return Err(e),
            };
            let mut work = store.get_work(&reference).await?;
            if work.dispatch_started || work.terminal() || work.execution != anchor.execution {
                owner.release().await?;
                continue;
            }
            work.executor = Some(anchor.reference());
            work.dispatch_started = true;
            work.dispatched_at = Some(crate::model::now().micros());
            work.record.updated_at = crate::model::now();
            owner.save(&work).await?;
            owner.release().await?;
            return Ok(Some(reference));
        }
        Ok(None)
    })
    .await;
    let released = lock.release().await;
    match result {
        Ok(next) => {
            released?;
            Ok(next)
        }
        Err(e) => Err(e),
    }
}
