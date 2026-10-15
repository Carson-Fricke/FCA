//! Kernel-owned filesystem locks survive host restarts and release on worker death.
use lake_contracts::DataError;
use std::{
    fs::{File, OpenOptions},
    path::Path,
    sync::Mutex,
};

pub struct FileOwnership {
    file: Mutex<Option<File>>,
}
impl FileOwnership {
    pub fn acquire(path: &Path) -> Result<Self, DataError> {
        Self::open(path, false)
    }
    pub fn acquire_wait(path: &Path) -> Result<Self, DataError> {
        Self::open(path, true)
    }
    fn open(path: &Path, wait: bool) -> Result<Self, DataError> {
        std::fs::create_dir_all(
            path.parent()
                .ok_or_else(|| DataError::value("lock needs a parent"))?,
        )?;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        if wait {
            file.lock()?;
        } else {
            file.try_lock()
                .map_err(|_| DataError::Conflict("local execution is already owned".into()))?;
        }
        Ok(Self {
            file: Mutex::new(Some(file)),
        })
    }
    pub fn renew(&self) -> Result<(), DataError> {
        if self
            .file
            .lock()
            .map_err(|e| DataError::Io(e.to_string()))?
            .is_none()
        {
            return Err(DataError::Conflict(
                "local execution ownership was released".into(),
            ));
        }
        Ok(())
    }
    pub fn release(&self) -> Result<(), DataError> {
        self.file
            .lock()
            .map_err(|e| DataError::Io(e.to_string()))?
            .take();
        Ok(())
    }
}
impl lake_storage::Lease for FileOwnership {
    fn lease_id(&self) -> Option<&str> {
        Some("local-kernel-lock")
    }
    fn renew(&self) -> Result<(), DataError> {
        self.renew()
    }
    fn release(&self) -> Result<(), DataError> {
        self.release()
    }
}
pub struct FileLeaseProvider(pub std::path::PathBuf);
impl lake_storage::LeaseProvider for FileLeaseProvider {
    fn acquire(
        &self,
        scope: &lake_storage::SymbolDayScope,
    ) -> Result<Box<dyn lake_storage::Lease>, DataError> {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
        loop {
            match FileOwnership::acquire(&self.0.join(scope.blob_name())) {
                Ok(owner) => return Ok(Box::new(owner)),
                Err(DataError::Conflict(_)) if std::time::Instant::now() < deadline => {
                    std::thread::sleep(std::time::Duration::from_millis(10))
                }
                Err(e) => return Err(e),
            }
        }
    }
}
