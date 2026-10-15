//! Finite local worker processes; launch receipts prohibit replay after unknown outcomes.
use crate::LocalJobStore;
use lake_contracts::DataError;
use lake_ingest_jobs::{GroupState, LocalConfig, WorkerRunner, WorkerSpec};
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
        if !config.executable.is_file() || config.max_workers == 0 || config.timeout_seconds < 60 {
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
    fn configuration(&self) -> WorkerSpec {
        self.config.clone().into()
    }
    fn for_work(&self, spec: &WorkerSpec) -> Result<Self, DataError> {
        match spec {
            WorkerSpec::Local(config) => Ok(Self {
                config: config.clone(),
                children: self.children.clone(),
            }),
            _ => Err(DataError::value("local runner cannot execute ACI work")),
        }
    }
    async fn inspect(&self, id: &str) -> Result<GroupState, DataError> {
        if let Some(child) = self
            .children
            .lock()
            .map_err(|e| DataError::Io(e.to_string()))?
            .get_mut(id)
        {
            return Ok(if child.try_wait()?.is_some() {
                GroupState::Stopped
            } else {
                GroupState::Active
            });
        }
        let store = LocalJobStore::new(self.config.root.clone());
        if store.is_owned(id)? {
            return Ok(GroupState::Active);
        }
        Ok(if self.receipt(id).exists() {
            GroupState::Stopped
        } else {
            GroupState::Absent
        })
    }
    async fn start(&self, id: &str) -> Result<(), DataError> {
        let path = self.receipt(id);
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
        let mut command = Command::new(&self.config.executable);
        command
            .arg(id)
            .env("HARNESS_MARKET_DATA_MODE", "local")
            .env("LAKE_LOCAL_DATA_DIR", &self.config.root)
            .env(
                "LAKE_INGEST_MAX_WORKERS",
                self.config.max_workers.to_string(),
            )
            .env(
                "LAKE_INGEST_TIMEOUT_SECONDS",
                self.config.timeout_seconds.to_string(),
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
            .insert(id.into(), child);
        Ok(())
    }
    async fn delete(&self, id: &str) -> Result<(), DataError> {
        if let Some(mut child) = self
            .children
            .lock()
            .map_err(|e| DataError::Io(e.to_string()))?
            .remove(id)
        {
            if child.try_wait()?.is_none() {
                child.kill()?;
            }
            child.wait()?;
        }
        if LocalJobStore::new(self.config.root.clone()).is_owned(id)? {
            return Err(DataError::Conflict(
                "local worker still owns execution".into(),
            ));
        }
        let path = self.receipt(id);
        if path.exists() {
            std::fs::remove_file(path)?;
        }
        Ok(())
    }
}
