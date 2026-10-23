//! Composes memory/local object adapters with the single execution store.
//! No alternate job schemas, result publication or lifecycle implementations.
mod local;
mod memory;
use harness_execution::store::ExecutionStore;
pub use local::Local;
pub use memory::Memory;
use std::{path::PathBuf, sync::Arc};

pub fn memory_store() -> ExecutionStore {
    ExecutionStore::new(Arc::new(Memory::default()))
}
pub fn filesystem_store(root: impl Into<PathBuf>) -> ExecutionStore {
    ExecutionStore::new(Arc::new(Local { root: root.into() }))
}
