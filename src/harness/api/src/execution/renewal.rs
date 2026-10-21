//! Worker keepalive state, independent of simulation and blocking storage calls.
use super::{backend::Result, store::ExecutionStore, worker::Slot};
use chrono::{DateTime, Utc};
use mcp::store::StoreError;
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

#[derive(Clone)]
struct JobLease {
    store: ExecutionStore,
    id: String,
    until: DateTime<Utc>,
}
struct State {
    fleet_until: DateTime<Utc>,
    job: Option<JobLease>,
    failure: Option<String>,
    last_error: Option<String>,
    stopped: bool,
}
struct Shared {
    state: Mutex<State>,
    changed: Condvar,
}

/// Owns the last acknowledged deadlines. Failed/ambiguous writes never extend them.
/// The process watchdog can check these while the renewal thread is blocked in I/O.
#[derive(Clone)]
pub struct Ownership {
    slot: Arc<Slot>,
    shared: Arc<Shared>,
}
impl Ownership {
    pub fn new(slot: Slot) -> Self {
        let fleet_until = slot.attached_until();
        Self {
            slot: Arc::new(slot),
            shared: Arc::new(Shared {
                state: Mutex::new(State {
                    fleet_until,
                    job: None,
                    failure: None,
                    last_error: None,
                    stopped: false,
                }),
                changed: Condvar::new(),
            }),
        }
    }
    pub fn track(&self, store: ExecutionStore, id: String) -> Result<()> {
        let until = store
            .claimed_until()
            .ok_or_else(|| StoreError::OwnershipLost(format!("unclaimed execution {id}")))?;
        self.shared.state.lock().unwrap().job = Some(JobLease { store, id, until });
        self.shared.changed.notify_all();
        Ok(())
    }
    pub fn clear(&self, id: &str) {
        let mut state = self.shared.state.lock().unwrap();
        if state.job.as_ref().is_some_and(|j| j.id == id) {
            state.job = None;
        }
        self.shared.changed.notify_all();
    }
    /// Fail closed on confirmed token loss or the earliest acknowledged expiry.
    pub fn check(&self, now: DateTime<Utc>) -> Result<Duration> {
        let state = self.shared.state.lock().unwrap();
        if let Some(failure) = &state.failure {
            return Err(StoreError::OwnershipLost(failure.clone()));
        }
        let (scope, until) = state
            .job
            .as_ref()
            .filter(|j| j.until < state.fleet_until)
            .map_or(("fleet", state.fleet_until), |j| (j.id.as_str(), j.until));
        (until - now)
            .to_std()
            .ok()
            .filter(|d| !d.is_zero())
            .ok_or_else(|| {
                StoreError::OwnershipLost(format!(
                    "{scope} deadline {until} reached; last renewal error: {}",
                    state
                        .last_error
                        .as_deref()
                        .unwrap_or("no acknowledged renewal")
                ))
            })
    }
    fn record_error(&self, scope: &str, id: Option<&str>, cause: StoreError) {
        let mut state = self.shared.state.lock().unwrap();
        if id.is_some_and(|id| !state.job.as_ref().is_some_and(|j| j.id == id)) {
            return;
        }
        let message = format!("{scope}: {cause}");
        eprintln!(
            "{}",
            serde_json::json!({
                "event": "worker_renewal_failed", "scope": scope, "job_id": id,
                "fleet_expires_at": state.fleet_until,
                "job_expires_at": state.job.as_ref().map(|j| j.until),
                "error": cause.to_string(), "category": format!("{cause:?}"),
            })
        );
        if matches!(
            cause,
            StoreError::OwnershipLost(_) | StoreError::JobNotFound(_)
        ) {
            state.failure = Some(message.clone());
        }
        state.last_error = Some(message);
    }
    /// One renewal attempt. Transient, contention and permission failures retain
    /// the real deadlines and are retried; they are never labeled ownership loss.
    pub fn renew(&self) -> Duration {
        let mut failed = false;
        match self.slot.renew() {
            Ok(until) => self.shared.state.lock().unwrap().fleet_until = until,
            Err(error) => {
                failed = true;
                self.record_error("fleet", None, error);
            }
        }
        let job = self.shared.state.lock().unwrap().job.clone();
        if let Some(job) = job {
            match job.store.heartbeat(&job.id) {
                Ok(until) => {
                    let mut state = self.shared.state.lock().unwrap();
                    if state.job.as_ref().is_some_and(|j| j.id == job.id) {
                        match until {
                            Some(until) => state.job.as_mut().unwrap().until = until,
                            None => state.job = None,
                        }
                    }
                }
                Err(error) => {
                    failed = true;
                    self.record_error("job", Some(&job.id), error);
                }
            }
        }
        if !failed {
            let mut state = self.shared.state.lock().unwrap();
            if state.last_error.take().is_some() {
                eprintln!(
                    "{}",
                    serde_json::json!({"event":"worker_renewal_recovered", "job_id": state.job.as_ref().map(|j| &j.id)})
                );
            }
        }
        if failed {
            Duration::from_secs(1)
        } else {
            Duration::from_secs(10)
        }
    }
    pub fn run(&self) {
        loop {
            if self.shared.state.lock().unwrap().stopped || self.check(Utc::now()).is_err() {
                return;
            }
            let delay = self.renew();
            let state = self.shared.state.lock().unwrap();
            if state.stopped {
                return;
            }
            drop(self.shared.changed.wait_timeout(state, delay).unwrap());
        }
    }
    pub fn stop(&self) {
        self.shared.state.lock().unwrap().stopped = true;
        self.shared.changed.notify_all();
    }
}
