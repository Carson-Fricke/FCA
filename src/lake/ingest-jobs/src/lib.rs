//! Durable ingestion admission and external execution. No providers or dataset decoding.
mod aca;
mod budget;
mod model;
mod service;
mod store;
pub use aca::{AcaConfig, AcaRunner, ExecutionState};
pub use budget::{budget_scope, BudgetGrant, BudgetState};
pub use model::{DurableJob, WorkRef, WorkRequest};
pub use service::IngestionJobs;
pub use store::{BlobJobRepository, JobStore, OwnedJob};

mod execution;
mod ports;
pub use execution::{LocalConfig, WorkerSpec};
pub use ports::{while_owned, ActiveJob, JobOwnership, JobPersistence, WorkerRunner};
