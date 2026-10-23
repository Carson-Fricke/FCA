//! Conditional metadata and immutable artifact adapters; no execution policy.
use harness_execution::jobs::StorageFailureKind;
use harness_execution::jobs::StoreError;
use lake_storage::AzureBlobContainer;
use std::sync::Arc;

pub use harness_execution::persistence::{error, Backend, Result};

pub struct Azure {
    pub container: Arc<dyn AzureBlobContainer>,
    pub sas: Arc<dyn lake_storage::SasIssuer>,
}
fn storage_error(
    operation: &'static str,
    path: &str,
    cause: lake_contracts::DataError,
) -> StoreError {
    use lake_contracts::DataError;
    let kind = match &cause {
        DataError::Conflict(_) => StorageFailureKind::Contention,
        DataError::Http {
            status: 401 | 403, ..
        } => StorageFailureKind::Permission,
        DataError::Http {
            status: 408 | 429 | 500..=599,
            ..
        }
        | DataError::Io(_) => StorageFailureKind::Transient,
        _ => StorageFailureKind::Permanent,
    };
    StoreError::Storage {
        kind,
        operation,
        path: path.into(),
        message: cause.to_string(),
    }
}
impl Backend for Azure {
    fn read_metadata(&self, path: &str) -> Result<Option<Vec<u8>>> {
        match self.container.download_versioned(path) {
            Ok((bytes, _)) => Ok(Some(bytes)),
            Err(lake_contracts::DataError::NotFound(_)) => Ok(None),
            Err(e) => Err(storage_error("read_metadata", path, e)),
        }
    }
    fn create_file(&self, path: &str, source: &std::path::Path) -> Result<()> {
        self.container
            .upload_file(path, source)
            .map_err(|e| storage_error("upload_artifact", path, e))
    }
    fn open(&self, path: &str) -> Result<Box<dyn std::io::Read + Send>> {
        let url = self.sas.issue(path).map_err(error)?;
        let agent = ureq::AgentBuilder::new()
            .redirects(0)
            .timeout_connect(std::time::Duration::from_secs(10))
            .timeout_read(std::time::Duration::from_secs(60))
            .build();
        let response = agent
            .get(&url)
            .call()
            .map_err(|_| error("artifact object transfer failed"))?;
        Ok(response.into_reader())
    }

    fn read(&self, path: &str) -> Result<Option<Vec<u8>>> {
        match self.container.download(path) {
            Ok(bytes) => Ok(Some(bytes)),
            Err(lake_contracts::DataError::NotFound(_)) => Ok(None),
            Err(e) => Err(storage_error("read", path, e)),
        }
    }
    fn create(&self, path: &str, bytes: &[u8]) -> Result<bool> {
        match self.container.upload(path, bytes, false) {
            Ok(()) => Ok(true),
            Err(lake_contracts::DataError::Conflict(_)) => Ok(false),
            Err(e) => Err(storage_error("create", path, e)),
        }
    }
    fn update(&self, path: &str, change: &mut dyn FnMut(Vec<u8>) -> Result<Vec<u8>>) -> Result<()> {
        let mut contention_deadline = None;
        let mut attempt = 0u64;
        loop {
            let (bytes, etag) = self
                .container
                .download_versioned(path)
                .map_err(|e| match e {
                    lake_contracts::DataError::NotFound(_) => StoreError::JobNotFound(path.into()),
                    e => storage_error("read_metadata", path, e),
                })?;
            let next = change(bytes.clone())?;
            if next == bytes {
                return Ok(());
            }
            match self.container.upload_if_match(path, &next, &etag) {
                Ok(()) => return Ok(()),
                Err(lake_contracts::DataError::Conflict(_))
                    if contention_deadline
                        .is_none_or(|deadline| std::time::Instant::now() < deadline) =>
                {
                    contention_deadline.get_or_insert_with(|| {
                        std::time::Instant::now() + std::time::Duration::from_secs(5)
                    });
                    attempt += 1;
                    // Spread competing writers without holding a remote lock.
                    std::thread::sleep(std::time::Duration::from_millis(10 + (attempt * 37) % 90));
                }
                Err(e) => return Err(storage_error("compare_exchange_metadata", path, e)),
            }
        }
    }
    fn list(&self, prefix: &str) -> Result<Vec<String>> {
        self.container
            .list(prefix)
            .map_err(|e| storage_error("list", prefix, e))
    }
    fn delete(&self, path: &str) -> Result<()> {
        self.container
            .delete(path)
            .map_err(|e| storage_error("delete", path, e))
    }
}
