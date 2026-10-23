//! Conditional persistence required by fenced execution.
//! Implementations own transport, storage and retry mechanics.
use crate::jobs::StoreError;

pub type Result<T> = std::result::Result<T, StoreError>;
pub trait Backend: Send + Sync {
    fn read(&self, path: &str) -> Result<Option<Vec<u8>>>;
    fn read_metadata(&self, path: &str) -> Result<Option<Vec<u8>>> {
        self.read(path)
    }
    fn open(&self, path: &str) -> Result<Box<dyn std::io::Read + Send>>;
    fn create(&self, path: &str, bytes: &[u8]) -> Result<bool>;
    fn create_file(&self, path: &str, source: &std::path::Path) -> Result<()>;
    /// Apply a conditional mutation. On version conflict, `change` is re-evaluated
    /// against current bytes; callbacks must reset attempt-local outputs and
    /// recognize their own committed tokens after an SDK retried a lost response.
    fn update(&self, path: &str, change: &mut dyn FnMut(Vec<u8>) -> Result<Vec<u8>>) -> Result<()>;
    fn list(&self, prefix: &str) -> Result<Vec<String>>;
    fn delete(&self, path: &str) -> Result<()>;
}
pub fn error(error: impl std::fmt::Display) -> StoreError {
    StoreError::Io(error.to_string())
}
