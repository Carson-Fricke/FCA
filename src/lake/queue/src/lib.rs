//! At-least-once queue processing with visibility renewal and dead-letter
//! recovery.
//!
//! Ports the provider-independent half of the Python `lake.queue` package.
//! [`worker`] owns delivery semantics for one leased message at a time; the
//! Azure queue transport and sender adapters arrive with the hosted phase and
//! implement the [`lake_storage::QueueSender`] boundary. The dead-letter
//! envelope and replay shapes are provider-independent and live in
//! [`dead_letter`].

pub mod dead_letter;
pub mod worker;

pub use dead_letter::{
    DeadLetterEnvelope, DeadLetterReplayer, DeadLetterStore, InMemoryDeadLetterStore,
    QueueDeadLetterSink, ReplayResult, DEAD_LETTER_SCHEMA_VERSION,
};
pub use worker::{
    DeadLetterSink, DeliveryOutcome, DeliveryResult, QueueDelivery, QueueHandler, QueueMessage,
    QueueTransport, QueueWorker, QueueWorkerPolicy, TokioWorkerClock, WorkerClock,
};
