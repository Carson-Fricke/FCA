//! Azure hosted composition for the published market-lake API and ingest.
//!
//! Ports `lake.hosted.runtime`: [`runtime`] builds the Azure Table catalog,
//! durable job repository, identity-based SAS issuer, and lease-protected
//! equity ingest from non-secret environment configuration; [`api`] selects
//! the Azure composition for the published `lake api` process when
//! `LAKE_STORAGE_ACCOUNT_NAME` is present and the in-memory one otherwise;
//! [`worker`] composes the queue worker over the Azure transport and the
//! option ingest queue handler.

pub mod api;
pub mod runtime;
pub mod worker;

pub use runtime::{
    hosted_overrides, HostedEnsureError, HostedOverrides, HostedRuntime, IngestSessions,
    DEFAULT_COVERAGE_TABLE, DEFAULT_JOBS_TABLE, DEFAULT_STORAGE_CONTAINER,
};
pub use worker::{compose_option_queue_worker, queue_worker_policy, OptionQueueHandlerAdapter};
