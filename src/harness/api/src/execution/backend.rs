//! Conditional metadata and immutable artifact adapters; no execution policy.
use lake_storage::AzureBlobContainer;
use mcp::store::StoreError;
use std::{path::PathBuf, sync::Arc};

pub type Result<T> = std::result::Result<T, StoreError>;
pub trait Backend: Send + Sync {
    fn read(&self, path: &str) -> Result<Option<Vec<u8>>>;
    fn open(&self, path: &str) -> Result<Box<dyn std::io::Read + Send>>;
    fn create(&self, path: &str, bytes: &[u8]) -> Result<bool>;
    fn create_file(&self, path: &str, source: &std::path::Path) -> Result<()>;
    fn update(&self, path: &str, change: &mut dyn FnMut(Vec<u8>) -> Result<Vec<u8>>) -> Result<()>;
    fn list(&self, prefix: &str) -> Result<Vec<String>>;
    fn delete(&self, path: &str) -> Result<()>;
}
pub fn error(error: impl std::fmt::Display) -> StoreError {
    StoreError::Io(error.to_string())
}

pub struct Local {
    pub root: PathBuf,
}
impl Local {
    fn lock(&self, path: &str) -> Result<std::fs::File> {
        use sha2::{Digest, Sha256};
        let dir = self.root.join("locks");
        std::fs::create_dir_all(&dir).map_err(error)?;
        let lock = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(dir.join(hex::encode(Sha256::digest(path.as_bytes()))))
            .map_err(error)?;
        lock.lock().map_err(error)?;
        Ok(lock)
    }
    fn replace(&self, path: &str, bytes: &[u8]) -> Result<()> {
        use std::io::Write;
        let target = self.root.join(path);
        let parent = target.parent().unwrap();
        std::fs::create_dir_all(parent).map_err(error)?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent).map_err(error)?;
        temporary.write_all(bytes).map_err(error)?;
        temporary.as_file().sync_all().map_err(error)?;
        temporary.persist(target).map_err(error)?;
        Ok(())
    }
}
impl Backend for Local {
    fn create_file(&self, path: &str, source: &std::path::Path) -> Result<()> {
        let _guard = self.lock(path)?;
        let target = self.root.join(path);
        if target.exists() {
            return Ok(());
        }
        let parent = target.parent().unwrap();
        std::fs::create_dir_all(parent).map_err(error)?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent).map_err(error)?;
        std::io::copy(
            &mut std::fs::File::open(source).map_err(error)?,
            temporary.as_file_mut(),
        )
        .map_err(error)?;
        temporary.as_file().sync_all().map_err(error)?;
        temporary.persist(target).map_err(error)?;
        Ok(())
    }

    fn open(&self, path: &str) -> Result<Box<dyn std::io::Read + Send>> {
        Ok(Box::new(
            std::fs::File::open(self.root.join(path)).map_err(error)?,
        ))
    }
    fn read(&self, path: &str) -> Result<Option<Vec<u8>>> {
        match std::fs::read(self.root.join(path)) {
            Ok(bytes) => Ok(Some(bytes)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(error(e)),
        }
    }
    fn create(&self, path: &str, bytes: &[u8]) -> Result<bool> {
        let _guard = self.lock(path)?;
        if self.read(path)?.is_some() {
            return Ok(false);
        }
        self.replace(path, bytes)?;
        Ok(true)
    }
    fn update(&self, path: &str, change: &mut dyn FnMut(Vec<u8>) -> Result<Vec<u8>>) -> Result<()> {
        let _guard = self.lock(path)?;
        let bytes = self
            .read(path)?
            .ok_or_else(|| StoreError::JobNotFound(path.into()))?;
        self.replace(path, &change(bytes)?)
    }
    fn list(&self, prefix: &str) -> Result<Vec<String>> {
        fn walk(
            root: &std::path::Path,
            dir: &std::path::Path,
            out: &mut Vec<String>,
        ) -> Result<()> {
            if !dir.exists() {
                return Ok(());
            }
            for entry in std::fs::read_dir(dir).map_err(error)? {
                let path = entry.map_err(error)?.path();
                if path.is_dir() {
                    walk(root, &path, out)?;
                } else {
                    out.push(
                        path.strip_prefix(root)
                            .map_err(error)?
                            .to_string_lossy()
                            .replace('\\', "/"),
                    );
                }
            }
            Ok(())
        }
        let mut out = Vec::new();
        walk(
            &self.root,
            &self.root.join(prefix.trim_end_matches('/')),
            &mut out,
        )?;
        out.sort();
        Ok(out)
    }
    fn delete(&self, path: &str) -> Result<()> {
        let _guard = self.lock(path)?;
        match std::fs::remove_file(self.root.join(path)) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(error(e)),
        }
    }
}

pub struct Azure {
    pub container: Arc<dyn AzureBlobContainer>,
    pub sas: Arc<dyn lake_storage::SasIssuer>,
}
impl Backend for Azure {
    fn create_file(&self, path: &str, source: &std::path::Path) -> Result<()> {
        self.container.upload_file(path, source).map_err(error)
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
            Err(e) => Err(error(e)),
        }
    }
    fn create(&self, path: &str, bytes: &[u8]) -> Result<bool> {
        match self.container.upload(path, bytes, false) {
            Ok(()) => Ok(true),
            Err(lake_contracts::DataError::Conflict(_)) => Ok(false),
            Err(e) => Err(error(e)),
        }
    }
    fn update(&self, path: &str, change: &mut dyn FnMut(Vec<u8>) -> Result<Vec<u8>>) -> Result<()> {
        // A short metadata lease fences the final write. Slow engine work never
        // runs under this lease; worker ownership is explicit application data.
        let mut lease = None;
        for attempt in 0..20 {
            match self.container.acquire_lease(path, 60) {
                Ok(id) => {
                    lease = Some(id);
                    break;
                }
                Err(lake_contracts::DataError::Conflict(_)) if attempt < 19 => {
                    std::thread::sleep(std::time::Duration::from_millis(50))
                }
                Err(e) => return Err(error(e)),
            }
        }
        let lease = lease.ok_or_else(|| error("metadata is busy"))?;
        let result = (|| {
            let bytes = self
                .read(path)?
                .ok_or_else(|| StoreError::JobNotFound(path.into()))?;
            let next = change(bytes)?;
            self.container
                .upload_leased(path, &next, &lease)
                .map_err(error)
        })();
        let released = self.container.release_lease(path, &lease).map_err(error);
        result.and(released)
    }
    fn list(&self, prefix: &str) -> Result<Vec<String>> {
        self.container.list(prefix).map_err(error)
    }
    fn delete(&self, path: &str) -> Result<()> {
        self.container.delete(path).map_err(error)
    }
}
