//! Finite worker composition, global slot admission and interrupted-owner recovery.
use super::backend::error;
use super::backend::Result;
use super::ExecutionStore;
use chrono::Utc;
use harness_execution::jobs::JobState;
use harness_execution::jobs::{JobAdmission, JobQueries, ResultPublisher};
use std::sync::Arc;

pub fn run() -> Result<()> {
    let store = super::configured_store()?;
    store.recover_interrupted()?;
    let max = std::env::var("HARNESS_EXECUTION_MAX_WORKERS")
        .ok()
        .map(|s| s.parse::<usize>().map_err(error))
        .transpose()?
        .unwrap_or(2);
    let slot = match std::env::var("HARNESS_EXECUTION_SLOT_TOKEN") {
        Ok(token) => Slot::attach(&store, token)?,
        Err(_) => {
            let Some(slot) = Slot::acquire(&store, max)? else {
                return Ok(());
            };
            slot
        }
    };
    let ownership = super::renewal::Ownership::new(slot);
    let renew = ownership.clone();
    let renewer = std::thread::spawn(move || renew.run());
    let work = ownership.clone();
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let result =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| run_jobs(store, work)))
                .unwrap_or_else(|_| Err(error("execution thread panicked")));
        let _ = sender.send(result);
    });
    let result = loop {
        // Storage renewal runs separately: a stalled SDK cannot stall fencing.
        let remaining = match ownership.check(Utc::now()) {
            Ok(remaining) => remaining,
            Err(cause) => {
                eprintln!(
                    "{}",
                    serde_json::json!({"event":"worker_ownership_lost", "error":cause.to_string()})
                );
                std::process::exit(2);
            }
        };
        match receiver.recv_timeout(remaining.min(std::time::Duration::from_millis(250))) {
            Ok(result) => break result,
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(cause) => break Err(error(cause)),
        }
    };
    ownership.stop();
    let _ = renewer.join();
    result
}

fn run_jobs(store: ExecutionStore, ownership: super::renewal::Ownership) -> Result<()> {
    let runtime = tokio::runtime::Runtime::new().map_err(error)?;
    let env = std::env::vars().collect();
    let host = runtime
        .block_on(async {
            lake_hosted::api::create_host(
                &env,
                Arc::new(|| lake_contracts::DateTime::from_micros(Utc::now().timestamp_micros())),
                "http://127.0.0.1/unused",
            )
        })
        .map_err(error)?;
    loop {
        ownership.check(Utc::now())?;
        store.recover_interrupted()?;
        let mut claimed = None;
        for job in store.queued()? {
            if let Some(owned) = store.claim(&job.job_id)? {
                claimed = Some((job, owned));
                break;
            }
        }
        let Some((job, owned)) = claimed else {
            break;
        };
        let id = job.job_id.clone();
        ownership.track(owned.clone(), id.clone())?;
        let access = host.access.clone();
        crate::local::execute_admitted(Arc::new(owned.clone()), job, access);
        // A failed read leaves ownership tracked; absence is not an I/O outcome.
        let record = owned
            .try_get(&id)?
            .ok_or_else(|| error("execution record disappeared"))?;
        if !record.state.is_terminal() {
            let mut failed = record;
            failed.state = JobState::Failed;
            failed.error_message =
                Some("execution worker terminated without a durable result".into());
            owned.put_result(failed, Default::default())?;
        }
        ownership.clear(&id);
    }
    Ok(())
}

/// Scheduled low-resource control process: recover dead owners, and notify the
/// finite pool if queued work remains. It never runs a compiler or simulation.
pub fn dispatch() -> Result<()> {
    let store = super::configured_store()?;
    store.recover_interrupted()?;
    if !store.queued()?.is_empty() {
        super::dispatch::notify_pool().map_err(error)?;
    }
    Ok(())
}

/// Deployment proof: an explicitly retained caller ID must acquire inputs,
/// execute outside this control process, and publish a durable successful result.
pub fn smoke() -> Result<()> {
    let id = std::env::var("HARNESS_SMOKE_JOB_ID")
        .map_err(|_| error("HARNESS_SMOKE_JOB_ID must be allocated by the smoke caller"))?;
    let store = super::configured_store()?;
    let record = crate::local::prepare_replay(
        std::path::Path::new("config/harness/smoke.yaml"),
        Some(&id),
        "backtest",
        harness_execution::jobs::JobKind::Backtest,
    )
    .map_err(error)?;
    store.admit(record)?;
    if let Err(error) = super::dispatch::notify_pool() {
        eprintln!("durable smoke remains admitted; capacity acknowledgement unavailable: {error}");
    }
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(540);
    loop {
        store.recover_interrupted()?;
        let record = store
            .try_get(&id)?
            .ok_or_else(|| error("smoke job disappeared"))?;
        if record.state == JobState::Succeeded {
            return Ok(());
        }
        if record.state.is_terminal() {
            return Err(error(
                record
                    .error_message
                    .as_deref()
                    .unwrap_or("smoke execution failed"),
            ));
        }
        if std::time::Instant::now() >= deadline {
            return Err(error(format!(
                "smoke deadline exceeded; reconcile retained job_id {id}"
            )));
        }
        std::thread::sleep(std::time::Duration::from_secs(2));
    }
}

use harness_execution::fleet::Slot;
