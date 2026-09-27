//! At-least-once queue processing with visibility renewal and dead-letter
//! recovery.
//!
//! Ports the Python `lake.queue` package. [`worker`] owns delivery semantics
//! for one leased message at a time; [`adapters`] provides the Azure queue
//! transport and sender that implement the [`QueueTransport`] and
//! [`lake_storage::QueueSender`] boundaries. The dead-letter envelope and
//! replay shapes are provider-independent and live in [`dead_letter`].

pub mod adapters;
pub mod dead_letter;
pub mod worker;

pub use adapters::{AzureQueueClient, AzureQueueSender, AzureQueueTransport};
pub use dead_letter::{
    DeadLetterEnvelope, DeadLetterReplayer, DeadLetterStore, InMemoryDeadLetterStore,
    QueueDeadLetterSink, ReplayResult, DEAD_LETTER_SCHEMA_VERSION,
};
pub use worker::{
    DeadLetterSink, DeliveryOutcome, DeliveryResult, QueueDelivery, QueueHandler, QueueMessage,
    QueueTransport, QueueWorker, QueueWorkerPolicy, TokioWorkerClock, WorkerClock,
};
