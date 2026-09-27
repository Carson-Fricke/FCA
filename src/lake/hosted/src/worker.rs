//! Hosted queue-worker composition for option ingest.
//!
//! Wraps [`lake_ingestion::OptionQueueHandler`] in the provider-independent
//! [`lake_queue::QueueHandler`] port and composes the shared queue worker over
//! the Azure Storage Queue transport, the typed queue settings, and the
//! dead-letter sink driven by an Azure queue sender.

use std::sync::Arc;

use futures_util::future::BoxFuture;
use lake_configuration::QueueSettings;
use lake_contracts::DataError;
use lake_ingestion::{OptionIngestTransaction, OptionQueueHandler};
use lake_queue::{
    AzureQueueClient, QueueDeadLetterSink, QueueDelivery, QueueHandler, QueueTransport,
    QueueWorker, QueueWorkerPolicy,
};
use lake_storage::QueueSender;

/// Adapter presenting [`lake_ingestion::OptionQueueHandler`] as a
/// [`QueueHandler`]: the delivery's decoded payload is handed to the option
/// handler and any failure requests an at-least-once retry.
pub struct OptionQueueHandlerAdapter {
    inner: Arc<OptionQueueHandler>,
}

impl OptionQueueHandlerAdapter {
    /// Build the adapter over the shared option handler.
    #[must_use]
    pub fn new(
        transaction: Arc<OptionIngestTransaction>,
        front_sets: Option<Arc<dyn lake_catalog::DailyFrontSetRepository>>,
    ) -> Self {
        Self {
            inner: Arc::new(OptionQueueHandler::new(transaction, front_sets)),
        }
    }
}

impl QueueHandler for OptionQueueHandlerAdapter {
    fn handle<'a>(&'a self, delivery: &'a QueueDelivery) -> BoxFuture<'a, Result<(), DataError>> {
        Box::pin(self.inner.handle(delivery.payload()))
    }
}

/// Compose the hosted option-ingest queue worker.
///
/// One message is processed at a time; exhausted deliveries transfer to the
/// dead-letter queue through the injected sender before the source message is
/// deleted.
#[allow(clippy::too_many_arguments)]
pub fn compose_option_queue_worker(
    queue: Arc<dyn AzureQueueClient>,
    transaction: Arc<OptionIngestTransaction>,
    front_sets: Option<Arc<dyn lake_catalog::DailyFrontSetRepository>>,
    settings: &QueueSettings,
    dead_letter_sender: Arc<dyn QueueSender>,
    source_queue: &str,
    idle_sleep_seconds: f64,
) -> Result<QueueWorker, DataError> {
    let transport: Arc<dyn QueueTransport> = Arc::new(lake_queue::AzureQueueTransport::new(queue));
    let handler: Arc<dyn QueueHandler> =
        Arc::new(OptionQueueHandlerAdapter::new(transaction, front_sets));
    let dead_letter: Arc<dyn lake_queue::DeadLetterSink> =
        Arc::new(QueueDeadLetterSink::new(dead_letter_sender, source_queue)?);
    QueueWorker::from_settings(
        transport,
        handler,
        settings,
        Some(dead_letter),
        None,
        idle_sleep_seconds,
    )
}

/// The policy derived from the typed queue settings.
pub fn queue_worker_policy(settings: &QueueSettings) -> Result<QueueWorkerPolicy, DataError> {
    QueueWorkerPolicy::from_settings(settings)
}
