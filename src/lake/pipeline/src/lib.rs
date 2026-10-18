//! Process-wide ingestion budgets shared by providers, encoding, and publication.
//! Limits are explicit accounting limits, not an RSS allocator. Session admission
//! reserves bounded merge/encoding headroom; page streams spill instead of retaining daily input.
use lake_contracts::DataError;
use serde::Serialize;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc, Condvar, Mutex, OnceLock,
};
use std::time::{Duration, Instant};
mod usage;

const MIB: usize = 1024 * 1024;
static GLOBAL: OnceLock<Arc<Pipeline>> = OnceLock::new();

#[derive(Clone, Debug, Serialize)]
pub struct Limits {
    pub cpu_slots: usize,
    pub response_bytes: usize,
    pub max_response_bytes: usize,
    pub session_bytes: usize,
    pub max_session_bytes: usize,
    pub publication_slots: usize,
    pub scratch_bytes: usize,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            cpu_slots: std::thread::available_parallelism().map_or(1, std::num::NonZeroUsize::get),
            response_bytes: 1024 * MIB,
            max_response_bytes: 8 * MIB,
            session_bytes: 4096 * MIB,
            max_session_bytes: 2048 * MIB,
            publication_slots: 2,
            scratch_bytes: 4 * 1024 * MIB,
        }
    }
}
impl Limits {
    /// Parse only the non-secret resource configuration shared by workers and replay.
    pub fn from_environment(
        env: &std::collections::BTreeMap<String, String>,
    ) -> Result<Self, DataError> {
        let defaults = Self::default();
        let value = |name: &str, default: usize| -> Result<usize, DataError> {
            env.get(name)
                .map(|v| v.parse::<usize>().map_err(DataError::io))
                .transpose()
                .map(|v| v.unwrap_or(default))
        };
        let memory = |name: &str, default: usize| -> Result<usize, DataError> {
            value(name, default / MIB)?
                .checked_mul(MIB)
                .ok_or_else(|| DataError::value("ingestion memory limit overflow"))
        };
        let limits = Self {
            cpu_slots: value("LAKE_INGEST_CPU_SLOTS", defaults.cpu_slots)?,
            response_bytes: memory("LAKE_INGEST_RESPONSE_MEMORY_MIB", defaults.response_bytes)?,
            max_response_bytes: memory(
                "LAKE_INGEST_MAX_RESPONSE_MIB",
                defaults.max_response_bytes,
            )?,
            session_bytes: memory("LAKE_INGEST_MEMORY_MIB", defaults.session_bytes)?,
            max_session_bytes: memory("LAKE_INGEST_SESSION_MIB", defaults.max_session_bytes)?,
            scratch_bytes: memory("LAKE_INGEST_SCRATCH_MIB", defaults.scratch_bytes)?,
            publication_slots: value(
                "LAKE_INGEST_PUBLICATION_CONCURRENCY",
                defaults.publication_slots,
            )?,
        };
        limits.validate()?;
        Ok(limits)
    }
    pub fn validate(&self) -> Result<(), DataError> {
        if self.cpu_slots == 0
            || self.publication_slots == 0
            || self.max_response_bytes == 0
            || self.scratch_bytes == 0
            || self
                .max_response_bytes
                .checked_mul(16)
                .and_then(|bytes| bytes.checked_add(2 * MIB))
                .is_none_or(|v| v > self.response_bytes - self.response_bytes / 4)
            || self.max_session_bytes < MIB
            || self.max_session_bytes > self.session_bytes
        {
            return Err(DataError::value(
                "invalid ingestion CPU, response, session, or publication limits",
            ));
        }
        Ok(())
    }
}

/// Timings count overlapping stage service time, not wall time. Waits are separate.
#[derive(Clone, Copy, Debug)]
pub enum Stage {
    Secrets,
    Coverage,
    Progress,
    Network,
    Decode,
    Normalize,
    Sort,
    Validate,
    Arrow,
    Derive,
    Parquet,
    Hash,
    LeaseWait,
    Upload,
    Manifest,
    RateWait,
    RateCache,
    RateRpc,
    RateCapacity,
    RateConflict,
    RatePacing,
    Spill,
}
const NAMES: [&str; 22] = [
    "secrets",
    "coverage",
    "progress",
    "network",
    "decode",
    "normalize",
    "sort",
    "validate",
    "arrow",
    "derive",
    "parquet",
    "hash",
    "lease_wait",
    "upload",
    "manifest",
    "rate_wait",
    "rate_cache",
    "rate_rpc",
    "rate_capacity",
    "rate_conflict",
    "rate_pacing",
    "spill",
];
#[derive(Default)]
struct Measurement {
    calls: AtomicU64,
    nanos: AtomicU64,
    bytes: AtomicU64,
    rows: AtomicU64,
}
#[derive(Serialize)]
struct StageSnapshot {
    calls: u64,
    seconds: f64,
    bytes: u64,
    rows: u64,
}

pub struct Pipeline {
    pub limits: Limits,
    stopped: Arc<AtomicBool>,
    cpu: Arc<Pool>,
    responses: Arc<Pool>,
    decoded: Arc<Pool>,
    sessions: Arc<Pool>,
    publications: Arc<Pool>,
    scratch: Arc<Pool>,
    commit: Mutex<()>,
    measurements: [Arc<Measurement>; 22],
}
impl Pipeline {
    pub fn new(limits: Limits) -> Result<Arc<Self>, DataError> {
        limits.validate()?;
        let stopped = Arc::new(AtomicBool::new(false));
        Ok(Arc::new(Self {
            cpu: Pool::new(limits.cpu_slots, stopped.clone()),
            responses: Pool::new(limits.response_bytes / 4, stopped.clone()),
            decoded: Pool::new(
                limits.response_bytes - limits.response_bytes / 4,
                stopped.clone(),
            ),
            sessions: Pool::new(limits.session_bytes, stopped.clone()),
            publications: Pool::new(limits.publication_slots, stopped.clone()),
            scratch: Pool::new(limits.scratch_bytes, stopped.clone()),
            limits,
            stopped,
            commit: Mutex::new(()),
            measurements: std::array::from_fn(|_| Arc::default()),
        }))
    }
    pub fn global() -> Arc<Self> {
        GLOBAL
            .get_or_init(|| Self::new(Limits::default()).expect("valid default limits"))
            .clone()
    }
    pub fn initialize(limits: Limits) -> Result<Arc<Self>, DataError> {
        let pipeline = Self::new(limits)?;
        GLOBAL
            .set(pipeline.clone())
            .map_err(|_| DataError::value("ingestion pipeline already initialized"))?;
        Ok(pipeline)
    }
    pub fn check(&self) -> Result<(), DataError> {
        if self.stopped.load(Ordering::Acquire) {
            Err(DataError::EnsureJob(
                "ingestion cancelled or ownership lost".into(),
            ))
        } else {
            Ok(())
        }
    }
    pub async fn cpu(&self) -> Result<Reservation, DataError> {
        self.cpu.acquire(1).await
    }
    pub fn cpu_blocking(&self) -> Result<Reservation, DataError> {
        self.cpu.acquire_blocking(1)
    }
    /// Reserve wire + decoded-page headroom until normalized records release the page.
    pub async fn response(&self) -> Result<Reservation, DataError> {
        self.responses.acquire(self.limits.max_response_bytes).await
    }
    /// Decode headroom follows the actual bounded body through page consumption.
    /// Separate pools ensure a decoder never waits for memory held by queued wire bodies.
    pub async fn decoded_response(&self, bytes: usize) -> Result<Reservation, DataError> {
        self.decoded
            .acquire(bytes.saturating_mul(16).saturating_add(2 * MIB))
            .await
    }
    pub async fn session(&self) -> Result<Reservation, DataError> {
        self.sessions.acquire(self.limits.max_session_bytes).await
    }
    /// Scratch space cannot wait for files retained by this same session.
    pub fn scratch(&self, bytes: usize) -> Result<Reservation, DataError> {
        self.scratch
            .take(bytes)?
            .ok_or_else(|| DataError::value("ingestion scratch capacity exhausted"))
    }
    pub fn publication(&self) -> Result<Reservation, DataError> {
        self.publications.acquire_blocking(1)
    }
    /// Serialize cancellation with the final visibility mutation. After this returns,
    /// no blocking task can publish, even when aborting its async caller did not stop it.
    pub fn cancel(&self) {
        let _commit = self.commit.lock().unwrap_or_else(|e| e.into_inner());
        self.stopped.store(true, Ordering::Release);
        for pool in [
            &self.cpu,
            &self.responses,
            &self.decoded,
            &self.sessions,
            &self.publications,
            &self.scratch,
        ] {
            pool.notify.notify_waiters();
            pool.changed.notify_all();
        }
    }
    pub fn commit<T>(&self, write: impl FnOnce() -> Result<T, DataError>) -> Result<T, DataError> {
        let _commit = self.commit.lock().map_err(DataError::io)?;
        self.check()?;
        write()
    }
    pub fn timer(&self, stage: Stage) -> StageTimer {
        StageTimer {
            measurement: self.measurements[stage as usize].clone(),
            started: Instant::now(),
            bytes: 0,
            rows: 0,
        }
    }
    /// Bound retained normalized input, leaving room for references, Arrow and Parquet.
    /// This is a visible limit, not a wait for memory held by this same dataset.
    pub fn check_dataset_bytes(&self, bytes: usize) -> Result<(), DataError> {
        self.check()?;
        if bytes > self.limits.max_session_bytes / 4 {
            return Err(DataError::value("dataset exceeds ingestion session memory limit; increase LAKE_INGEST_SESSION_MIB with matching worker memory allocation"));
        }
        Ok(())
    }
    pub fn snapshot(&self) -> serde_json::Value {
        let stages: std::collections::BTreeMap<_, _> = NAMES
            .iter()
            .zip(&self.measurements)
            .map(|(name, m)| {
                (
                    *name,
                    StageSnapshot {
                        calls: m.calls.load(Ordering::Relaxed),
                        seconds: m.nanos.load(Ordering::Relaxed) as f64 / 1e9,
                        bytes: m.bytes.load(Ordering::Relaxed),
                        rows: m.rows.load(Ordering::Relaxed),
                    },
                )
            })
            .collect();
        serde_json::json!({"limits":self.limits, "process":usage::process_usage(), "stages":stages, "resources":{
            "cpu":self.cpu.snapshot(), "responses":self.responses.snapshot(), "decoded":self.decoded.snapshot(), "sessions":self.sessions.snapshot(), "publications":self.publications.snapshot(), "scratch": self.scratch.snapshot()}})
    }
}

pub struct StageTimer {
    measurement: Arc<Measurement>,
    started: Instant,
    bytes: u64,
    rows: u64,
}
impl StageTimer {
    pub fn bytes(&mut self, bytes: usize) {
        self.bytes = bytes as u64;
    }
    pub fn rows(&mut self, rows: usize) {
        self.rows = rows as u64;
    }
}
impl Drop for StageTimer {
    fn drop(&mut self) {
        self.measurement.calls.fetch_add(1, Ordering::Relaxed);
        self.measurement.nanos.fetch_add(
            self.started.elapsed().as_nanos().min(u64::MAX as u128) as u64,
            Ordering::Relaxed,
        );
        self.measurement
            .bytes
            .fetch_add(self.bytes, Ordering::Relaxed);
        self.measurement
            .rows
            .fetch_add(self.rows, Ordering::Relaxed);
    }
}

struct Pool {
    capacity: usize,
    state: Mutex<(usize, usize)>,
    stopped: Arc<AtomicBool>,
    notify: tokio::sync::Notify,
    changed: Condvar,
    wait_nanos: AtomicU64,
    waiting: AtomicU64,
}
impl Pool {
    fn new(capacity: usize, stopped: Arc<AtomicBool>) -> Arc<Self> {
        Arc::new(Self {
            capacity,
            state: Mutex::new((0, 0)),
            stopped,
            notify: tokio::sync::Notify::new(),
            changed: Condvar::new(),
            wait_nanos: AtomicU64::new(0),
            waiting: AtomicU64::new(0),
        })
    }
    fn take(self: &Arc<Self>, units: usize) -> Result<Option<Reservation>, DataError> {
        if self.stopped.load(Ordering::Acquire) {
            return Err(DataError::EnsureJob("ingestion cancelled".into()));
        }
        if units > self.capacity {
            return Err(DataError::value(
                "resource request exceeds configured capacity",
            ));
        }
        let mut state = self.state.lock().map_err(DataError::io)?;
        if units <= self.capacity - state.0 {
            state.0 += units;
            state.1 = state.1.max(state.0);
            Ok(Some(Reservation {
                pool: self.clone(),
                units,
            }))
        } else {
            Ok(None)
        }
    }
    async fn acquire(self: &Arc<Self>, units: usize) -> Result<Reservation, DataError> {
        let _wait = AdmissionWait::new(self.clone());
        loop {
            let notified = self.notify.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if let Some(permit) = self.take(units)? {
                return Ok(permit);
            }
            notified.await;
        }
    }
    fn acquire_blocking(self: &Arc<Self>, units: usize) -> Result<Reservation, DataError> {
        let _wait = AdmissionWait::new(self.clone());
        loop {
            if let Some(permit) = self.take(units)? {
                return Ok(permit);
            }
            let state = self.state.lock().map_err(DataError::io)?;
            if units <= self.capacity - state.0 {
                continue;
            }
            drop(
                self.changed
                    .wait_timeout(state, Duration::from_millis(25))
                    .map_err(DataError::io)?,
            );
        }
    }
    fn snapshot(&self) -> serde_json::Value {
        let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        serde_json::json!({"active":state.0, "peak":state.1, "waiting":self.waiting.load(Ordering::Relaxed), "capacity":self.capacity, "wait_seconds":self.wait_nanos.load(Ordering::Relaxed) as f64 / 1e9})
    }
}

struct AdmissionWait {
    pool: Arc<Pool>,
    started: Instant,
}
impl AdmissionWait {
    fn new(pool: Arc<Pool>) -> Self {
        pool.waiting.fetch_add(1, Ordering::Relaxed);
        Self {
            pool,
            started: Instant::now(),
        }
    }
}
impl Drop for AdmissionWait {
    fn drop(&mut self) {
        self.pool.waiting.fetch_sub(1, Ordering::Relaxed);
        self.pool.wait_nanos.fetch_add(
            self.started.elapsed().as_nanos().min(u64::MAX as u128) as u64,
            Ordering::Relaxed,
        );
    }
}

/// A reservation follows its actual allocation into blocking work and releases on drop.
pub struct Reservation {
    pool: Arc<Pool>,
    units: usize,
}
impl std::fmt::Debug for Reservation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Reservation")
            .field("units", &self.units)
            .finish()
    }
}
impl Drop for Reservation {
    fn drop(&mut self) {
        self.pool.state.lock().unwrap_or_else(|e| e.into_inner()).0 -= self.units;
        self.pool.notify.notify_waiters();
        self.pool.changed.notify_all();
    }
}
