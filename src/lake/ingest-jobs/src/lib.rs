//! Durable ingestion admission and external execution. No providers or dataset decoding.
mod aci;
mod model;
mod service;
mod store;
pub use aci::{AciConfig, AciRunner, GroupState};
pub use model::{DurableJob, WorkRequest};
pub use service::IngestionJobs;
pub use store::{BlobJobRepository, JobStore, OwnedJob};

mod execution;
mod ports;
pub use execution::{LocalConfig, WorkerSpec};
pub use ports::{while_owned, ActiveJob, JobOwnership, JobPersistence, WorkerRunner};
