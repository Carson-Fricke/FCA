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
struct FilePublicationLease {
    owner: FileOwnership,
    objects: lake_storage::LocalFilesystemStore,
    path: String,
}
impl lake_storage::Lease for FilePublicationLease {
    fn renewal_interval(&self) -> Option<std::time::Duration> {
        None
    }
    fn read(&self) -> Result<Vec<u8>, DataError> {
        use lake_storage::ReadableBlobStore;
        self.owner.renew()?;
        match self.objects.read(&self.path) {
            Err(DataError::NotFound(_)) => Ok(b"[]".to_vec()),
            result => result,
        }
    }
    fn publish(&self, payload: &[u8]) -> Result<(), DataError> {
        use lake_storage::BlobStore;
        let owner = self.owner.file.lock().map_err(DataError::io)?;
        if owner.is_none() {
            return Err(DataError::Conflict(
                "publication ownership was released".into(),
            ));
        }
        self.objects.replace(&self.path, payload)
    }
    fn lease_id(&self) -> Option<&str> {
        Some("local-kernel-lock")
    }
    fn renew(&self) -> Result<(), DataError> {
        self.owner.renew()
    }
    fn release(&self) -> Result<(), DataError> {
        self.owner.release()
    }
}
pub struct FileLeaseProvider(pub std::path::PathBuf);
impl lake_storage::LeaseProvider for FileLeaseProvider {
    fn acquire(
        &self,
        scope: &lake_storage::SymbolDayScope,
    ) -> Result<Box<dyn lake_storage::Lease>, DataError> {
        let owner =
            FileOwnership::acquire(&self.0.join(".publication-locks").join(scope.blob_name()))?;
        Ok(Box::new(FilePublicationLease {
            owner,
            objects: lake_storage::LocalFilesystemStore::new(self.0.clone()),
            path: format!("{}/{}", lake_catalog::PUBLICATION_PREFIX, scope.blob_name()),
        }))
    }
}
