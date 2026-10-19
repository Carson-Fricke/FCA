//! Finite worker composition, global slot admission and interrupted-owner recovery.
use super::{
    backend::{error, Result},
    store::{decode, encode},
    ExecutionStore,
};
use chrono::Utc;
use mcp::store::{JobState, JobStore};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

#[derive(Serialize, Deserialize)]
struct Fleet {
    owners: Vec<PoolOwner>,
}
#[derive(Serialize, Deserialize)]
struct PoolOwner {
    token: String,
    expires: chrono::DateTime<Utc>,
    #[serde(deserialize_with = "serde::Deserialize::deserialize")]
    worker: Option<String>,
}
pub struct Slot {
    store: ExecutionStore,
    token: String,
    worker: String,
}
impl Slot {
    /// Reserve before starting compute, including its cold boot interval.
    pub fn reserve(store: &ExecutionStore, max: usize) -> Result<Option<String>> {
        if max == 0 || max > 16 {
            return Err(error("execution fleet size must be 1..16"));
        }
        store.backend.create(
            "execution/control/fleet.json",
            &encode(&Fleet { owners: Vec::new() })?,
        )?;
        let token = uuid::Uuid::new_v4().to_string();
        let mut claimed = false;
        store
            .backend
            .update("execution/control/fleet.json", &mut |bytes| {
                let mut fleet: Fleet = decode(&bytes)?;
                fleet.owners.retain(|o| o.expires > Utc::now());
                if fleet.owners.len() < max {
                    fleet.owners.push(PoolOwner {
                        token: token.clone(),
                        expires: Utc::now() + chrono::Duration::seconds(600),
                        worker: None,
                    });
                    claimed = true;
                }
                encode(&fleet)
            })?;
        Ok(claimed.then_some(token))
    }
    pub fn attach(store: &ExecutionStore, token: String) -> Result<Self> {
        let worker = uuid::Uuid::new_v4().to_string();
        store
            .backend
            .update("execution/control/fleet.json", &mut |bytes| {
                let mut fleet: Fleet = decode(&bytes)?;
                let owner = fleet
                    .owners
                    .iter_mut()
                    .find(|owner| {
                        owner.token == token && owner.expires > Utc::now() && owner.worker.is_none()
                    })
                    .ok_or_else(|| error("worker reservation expired or was already consumed"))?;
                owner.worker = Some(worker.clone());
                owner.expires = Utc::now() + chrono::Duration::seconds(60);
                encode(&fleet)
            })?;
        Ok(Self {
            store: store.clone(),
            token,
            worker,
        })
    }
    pub fn acquire(store: &ExecutionStore, max: usize) -> Result<Option<Self>> {
        Self::reserve(store, max)?
            .map(|token| Self::attach(store, token))
            .transpose()
    }
    pub fn release_unstarted(store: &ExecutionStore, token: &str) -> Result<()> {
        store
            .backend
            .update("execution/control/fleet.json", &mut |bytes| {
                let mut fleet: Fleet = decode(&bytes)?;
                fleet
                    .owners
                    .retain(|owner| owner.token != token || owner.worker.is_some());
                encode(&fleet)
            })
    }
    pub fn renew(&self) -> Result<()> {
        self.store
            .backend
            .update("execution/control/fleet.json", &mut |bytes| {
                let mut fleet: Fleet = decode(&bytes)?;
                let owner = fleet
                    .owners
                    .iter_mut()
                    .find(|o| {
                        o.token == self.token
                            && o.worker.as_deref() == Some(&self.worker)
                            && o.expires > Utc::now()
                    })
                    .ok_or_else(|| error("execution fleet slot expired"))?;
                owner.expires = Utc::now() + chrono::Duration::seconds(60);
                encode(&fleet)
            })
    }
}
impl Drop for Slot {
    fn drop(&mut self) {
        let _ = self
            .store
            .backend
            .update("execution/control/fleet.json", &mut |bytes| {
                let mut fleet: Fleet = decode(&bytes)?;
                fleet
                    .owners
                    .retain(|o| o.token != self.token || o.worker.as_deref() != Some(&self.worker));
                encode(&fleet)
            });
    }
}

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
        slot.renew()?;
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
        let access = host.access.clone();
        let executor = owned.clone();
        let (sender, receiver) = std::sync::mpsc::channel();
        // The worker process, not the API, hosts JIT code. Losing its heartbeat
        // terminates this process; an old token cannot publish a terminal result.
        std::thread::spawn(move || {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                crate::local::execute_admitted(Arc::new(executor), job, access)
            }));
            let _ = sender.send(result);
        });
        loop {
            match receiver.recv_timeout(std::time::Duration::from_secs(10)) {
                Ok(_) => break,
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                    if slot.renew().and_then(|_| owned.heartbeat(&id)).is_err() {
                        eprintln!("worker ownership lost; stopping execution process");
                        std::process::exit(2);
                    }
                }
                Err(error) => return Err(super::backend::error(error)),
            }
        }
        if owned.get(&id).is_some_and(|r| !r.state.is_terminal()) {
            let mut failed = owned
                .get(&id)
                .ok_or_else(|| error("execution record disappeared"))?;
            failed.state = JobState::Failed;
            failed.error_message =
                Some("execution worker terminated without a durable result".into());
            owned.put_result(failed, Default::default())?;
        }
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
        mcp::store::JobKind::Backtest,
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
