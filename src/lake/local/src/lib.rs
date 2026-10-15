//! Local filesystem and finite-process adapters for the complete harness deployment.
mod catalog;
mod jobs;
mod ownership;
mod runner;
mod state;
pub use catalog::FileCatalog;
pub use jobs::LocalJobStore;
pub use ownership::FileLeaseProvider;
pub use runner::LocalRunner;
pub use state::{LocalLayout, LocalStateError, MARKER_CONTENT, MARKER_NAME};
