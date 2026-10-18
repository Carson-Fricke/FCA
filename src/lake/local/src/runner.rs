//! Finite local worker processes; launch receipts prohibit replay after unknown outcomes.
use crate::LocalJobStore;
use lake_contracts::DataError;
use lake_ingest_jobs::{ExecutionState, LocalConfig, WorkRef, WorkerRunner, WorkerSpec};
use std::{
    collections::BTreeMap,
    fs::OpenOptions,
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
};

#[derive(Clone)]
pub struct LocalRunner {
    pub config: LocalConfig,
    children: Arc<Mutex<BTreeMap<String, Child>>>,
}
impl LocalRunner {
    pub fn new(mut config: LocalConfig) -> Result<Self, DataError> {
        if !config.executable.is_file()
            || config.max_workers == 0
            || config.timeout_seconds < 60
            || config.partition_days == 0
            || config.session_concurrency == 0
        {
            return Err(DataError::value(
                "local worker executable and valid execution limits are required",
            ));
        }
        use lake_storage::BlobStore;
        use sha2::{Digest, Sha256};
        let bytes = std::fs::read(&config.executable)?;
        let digest = hex::encode(Sha256::digest(&bytes));
        let name = if cfg!(windows) {
            "lake-ingest-worker.exe"
        } else {
            "lake-ingest-worker"
        };
        let relative = format!("ingestion/bin/{digest}/{name}");
        let pinned = config.root.join(&relative);
        if !pinned.is_file() {
            lake_storage::LocalFilesystemStore::new(&config.root).replace(&relative, &bytes)?;
            std::fs::set_permissions(
                &pinned,
                std::fs::metadata(&config.executable)?.permissions(),
            )?;
        } else if std::fs::read(&pinned)? != bytes {
            return Err(DataError::value("pinned local worker is corrupt"));
        }
        config.executable = pinned;
        Ok(Self {
            config,
            children: Arc::default(),
        })
    }
    fn receipt(&self, id: &str) -> std::path::PathBuf {
        self.config
            .root
            .join("ingestion/launches")
            .join(format!("{id}.started"))
    }
}
#[async_trait::async_trait]
impl WorkerRunner for LocalRunner {
    fn begin_reconcile(&self) {}
    fn configuration(&self) -> WorkerSpec {
        self.config.clone().into()
    }
    fn for_work(&self, spec: &WorkerSpec) -> Result<Self, DataError> {
        match spec {
            WorkerSpec::Local(config) => Ok(Self {
                config: config.clone(),
                children: self.children.clone(),
            }),
            _ => Err(DataError::value("local runner cannot execute ACA work")),
        }
    }
    async fn inspect(&self, work: &WorkRef) -> Result<ExecutionState, DataError> {
        let id = work.worker_key()?;
        if let Some(child) = self
            .children
            .lock()
            .map_err(|e| DataError::Io(e.to_string()))?
            .get_mut(&id)
        {
            return Ok(if child.try_wait()?.is_some() {
                ExecutionState::Stopped
            } else {
                ExecutionState::Active
            });
        }
        let store = LocalJobStore::new(self.config.root.clone());
        if store.is_owned(work)? {
            return Ok(ExecutionState::Active);
        }
        Ok(if self.receipt(&id).exists() {
            ExecutionState::Stopped
        } else {
            ExecutionState::Absent
        })
    }
    async fn start(&self, work: &WorkRef) -> Result<(), DataError> {
        let id = work.worker_key()?;
        let partition = work
            .partition
            .as_ref()
            .ok_or_else(|| DataError::value("only an admitted partition can start"))?;
        let path = self.receipt(&id);
        std::fs::create_dir_all(
            path.parent()
                .ok_or_else(|| DataError::value("invalid receipt path"))?,
        )?;
        match OpenOptions::new().create_new(true).write(true).open(&path) {
            Ok(file) => file.sync_all()?,
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => return Ok(()),
            Err(e) => return Err(e.into()),
        }
        let logs = self.config.root.join("ingestion/logs");
        std::fs::create_dir_all(&logs)?;
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(logs.join(format!("{id}.log")))?;
        let scratch = self.config.root.join("ingestion/scratch").join(&id);
        std::fs::create_dir_all(&scratch)?;
        let mut command = Command::new(&self.config.executable);
        let host_memory = self.config.host_memory_mib;
        let worker_memory = host_memory
            .checked_div(self.config.max_workers)
            .ok_or_else(|| DataError::value("local worker count must be positive"))?;
        let session_pool = worker_memory
            .checked_mul(1)
            .ok_or_else(|| DataError::value("local host memory budget overflow"))?
            / 3;
        let response_pool = (worker_memory / 2).min(2048);
        if session_pool / self.config.session_concurrency == 0 || response_pool < 64 {
            return Err(DataError::value(
                "local host memory budget is too small for the configured worker fleet",
            ));
        }
        command
            .arg(&work.job_id)
            .arg(partition)
            .env("HARNESS_MARKET_DATA_MODE", "local")
            .env("TMP", &scratch)
            .env("TEMP", &scratch)
            .env("TMPDIR", &scratch)
            .env("LAKE_LOCAL_DATA_DIR", &self.config.root)
            .env(
                "LAKE_INGEST_MEMORY_MIB",
                std::env::var("LAKE_INGEST_MEMORY_MIB")
                    .unwrap_or_else(|_| session_pool.to_string()),
            )
            .env(
                "LAKE_INGEST_SESSION_MIB",
                std::env::var("LAKE_INGEST_SESSION_MIB").unwrap_or_else(|_| {
                    (session_pool / self.config.session_concurrency).to_string()
                }),
            )
            .env(
                "LAKE_INGEST_RESPONSE_MEMORY_MIB",
                std::env::var("LAKE_INGEST_RESPONSE_MEMORY_MIB")
                    .unwrap_or_else(|_| response_pool.to_string()),
            )
            .env(
                "LAKE_INGEST_CPU_SLOTS",
                std::env::var("LAKE_INGEST_CPU_SLOTS").unwrap_or_else(|_| {
                    std::thread::available_parallelism()
                        .map_or(1, std::num::NonZeroUsize::get)
                        .checked_div(self.config.max_workers)
                        .unwrap_or(1)
                        .max(1)
                        .to_string()
                }),
            )
            .env(
                "LAKE_INGEST_MAX_WORKERS",
                self.config.max_workers.to_string(),
            )
            .env(
                "LAKE_INGEST_TIMEOUT_SECONDS",
                self.config.timeout_seconds.to_string(),
            )
            .env(
                "LAKE_INGEST_SESSION_CONCURRENCY",
                self.config.session_concurrency.to_string(),
            )
            .stdin(Stdio::null())
            .stdout(log.try_clone()?)
            .stderr(log);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        if let Some(path) = &self.config.provider_env {
            command.env("LAKE_LOCAL_PROVIDER_ENV", path);
        }
        let child = command.spawn()?;
        self.children
            .lock()
            .map_err(|e| DataError::Io(e.to_string()))?
            .insert(id, child);
        Ok(())
    }
    async fn delete(&self, work: &WorkRef) -> Result<(), DataError> {
        let id = work.worker_key()?;
        if let Some(mut child) = self
            .children
            .lock()
            .map_err(|e| DataError::Io(e.to_string()))?
            .remove(&id)
        {
            if child.try_wait()?.is_none() {
                child.kill()?;
            }
            child.wait()?;
        }
        if LocalJobStore::new(self.config.root.clone()).is_owned(work)? {
            return Err(DataError::Conflict(
                "local worker still owns execution".into(),
            ));
        }
        let scratch_root = self.config.root.join("ingestion/scratch");
        let scratch = scratch_root.join(&id);
        if scratch.exists() {
            let root = std::fs::canonicalize(&scratch_root)?;
            let target = std::fs::canonicalize(&scratch)?;
            if !target.starts_with(&root) || target == root {
                return Err(DataError::value("scratch cleanup escaped its owned root"));
            }
            tokio::task::spawn_blocking(move || std::fs::remove_dir_all(target))
                .await
                .map_err(DataError::io)??;
        }
        let path = self.receipt(&id);
        if path.exists() {
            std::fs::remove_file(path)?;
        }
        Ok(())
    }
}
