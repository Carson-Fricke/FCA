//! Owns trader supervision snapshots; lifecycle coordination remains in the parent.
use std::cell::RefCell;
use std::path::PathBuf;
use std::rc::Rc;

use crate::runtime::state_store::InMemorySnapshotStore;
use crate::runtime::state_store::SnapshotObjectStore;

/// Shared, cheaply cloneable snapshot object store.
///
/// Wraps a store behind `Rc<RefCell<_>>` so the supervision journal and the
/// runtime state store observe one another's writes within a single process.
#[derive(Clone)]
pub struct SharedSnapshotStore {
    inner: Rc<RefCell<Box<dyn SnapshotObjectStore>>>,
}

impl SharedSnapshotStore {
    /// Wrap a store.
    #[must_use]
    pub fn new(store: Box<dyn SnapshotObjectStore>) -> Self {
        Self {
            inner: Rc::new(RefCell::new(store)),
        }
    }

    /// Build an in-memory shared store.
    #[must_use]
    pub fn in_memory() -> Self {
        Self::new(Box::new(InMemorySnapshotStore::new()))
    }

    /// Write one UTF-8 object.
    pub fn write_text(&self, path: &str, text: &str) {
        self.inner.borrow_mut().replace(path, text.as_bytes());
    }

    /// Read one UTF-8 object.
    #[must_use]
    pub fn read_text(&self, path: &str) -> Option<String> {
        self.inner
            .borrow()
            .read(path)
            .and_then(|bytes| String::from_utf8(bytes).ok())
    }
}

impl SnapshotObjectStore for SharedSnapshotStore {
    fn replace(&mut self, relative_path: &str, payload: &[u8]) {
        self.inner.borrow_mut().replace(relative_path, payload);
    }

    fn read(&self, relative_path: &str) -> Option<Vec<u8>> {
        self.inner.borrow().read(relative_path)
    }
}

/// A local filesystem snapshot store for the trader binary.
pub struct LocalFileSnapshotStore {
    root: PathBuf,
}

impl LocalFileSnapshotStore {
    /// Build a store rooted at `root`.
    #[must_use]
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub(super) fn resolve(&self, relative_path: &str) -> PathBuf {
        self.root.join(relative_path)
    }
}

impl SnapshotObjectStore for LocalFileSnapshotStore {
    fn replace(&mut self, relative_path: &str, payload: &[u8]) {
        let path = self.resolve(relative_path);
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = std::fs::write(path, payload);
    }

    fn read(&self, relative_path: &str) -> Option<Vec<u8>> {
        std::fs::read(self.resolve(relative_path)).ok()
    }
}
