//! At-least-once queue processing for durable market-lake ingest work.
//!
//! Ports `lake.queue.worker`. The worker owns queue delivery semantics, not
//! the ingest side effect: its handler must make deterministic, idempotent
//! writes, because a completed handler can be redelivered when a process
//! crashes before deleting its message. One leased message is processed at a
//! time; a renewal loop extends the visibility lease while the handler runs,
//! and retry exhaustion transfers the message to the injected dead-letter
//! boundary before the source message is deleted.
//!
//! Handler and boundary failures are [`DataError`]s, where Python allowed any
//! exception; the error text keeps the Python `"{TypeName}: {detail}"` shape
//! so dead-letter envelopes stay comparable.

use std::fmt;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

use futures_util::future::BoxFuture;
use lake_configuration::QueueSettings;
use lake_contracts::DataError;
use serde_json::Value;

/// The lease-bearing fields needed by a queue worker.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct QueueMessage {
    /// The provider message identifier.
    pub message_id: String,
    /// The provider lease receipt for the current receive.
    pub pop_receipt: String,
    /// The raw message body.
    pub content: String,
    /// The provider delivery count, starting at one.
    pub dequeue_count: u32,
}

impl QueueMessage {
    /// Build a validated lease-bearing message.
    pub fn new(
        message_id: impl Into<String>,
        pop_receipt: impl Into<String>,
        content: impl Into<String>,
        dequeue_count: u32,
    ) -> Result<Self, DataError> {
        let message_id = message_id.into();
        let pop_receipt = pop_receipt.into();
        let content = content.into();
        if message_id.trim().is_empty() {
            return Err(DataError::value("message_id must not be empty"));
        }
        if pop_receipt.trim().is_empty() {
            return Err(DataError::value("pop_receipt must not be empty"));
        }
        if dequeue_count < 1 {
            return Err(DataError::value("dequeue_count must be at least 1"));
        }
        Ok(Self {
            message_id,
            pop_receipt,
            content,
            dequeue_count,
        })
    }
}

/// Decoded handler input together with the original delivery metadata.
#[derive(Clone, Debug)]
pub struct QueueDelivery {
    message: QueueMessage,
    payload: Value,
}

impl QueueDelivery {
    fn new(message: QueueMessage, payload: Value) -> Self {
        Self { message, payload }
    }

    /// Build a delivery from its message metadata and decoded payload.
    pub fn from_parts(message: QueueMessage, payload: Value) -> Self {
        Self { message, payload }
    }

    /// The original delivery metadata.
    pub fn message(&self) -> &QueueMessage {
        &self.message
    }

    /// The decoded JSON-object payload.
    pub fn payload(&self) -> &Value {
        &self.payload
    }

    /// The provider message identifier.
    pub fn message_id(&self) -> &str {
        &self.message.message_id
    }

    /// The provider delivery count for this attempt.
    pub fn attempt(&self) -> u32 {
        self.message.dequeue_count
    }
}

/// An idempotent async operation for one decoded queue message.
///
/// The existing option ingest handler (`lake_ingestion::OptionQueueHandler`,
/// which takes the decoded payload and fails only for malformed messages)
/// plugs in through this boundary: apply the delivery or fail to request an
/// at-least-once retry.
pub trait QueueHandler: Send + Sync {
    /// Apply one delivery or fail to request an at-least-once retry.
    fn handle<'a>(&'a self, delivery: &'a QueueDelivery) -> BoxFuture<'a, Result<(), DataError>>;
}

/// Lease-aware queue boundary used by [`QueueWorker`].
pub trait QueueTransport: Send + Sync {
    /// Receive one message and make it invisible for the given duration.
    fn receive_one<'a>(
        &'a self,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<'a, Result<Option<QueueMessage>, DataError>>;

    /// Renew a message lease and return its updated pop receipt.
    fn renew<'a>(
        &'a self,
        message: &'a QueueMessage,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<'a, Result<QueueMessage, DataError>>;

    /// Delete a successfully handled or dead-lettered source message.
    fn delete<'a>(&'a self, message: &'a QueueMessage) -> BoxFuture<'a, Result<(), DataError>>;
}

/// Boundary for durable poison-message transfer.
pub trait DeadLetterSink: Send + Sync {
    /// Persist a failed delivery without deleting the source first.
    fn send<'a>(
        &'a self,
        message: &'a QueueMessage,
        error: &'a str,
    ) -> BoxFuture<'a, Result<(), DataError>>;
}

/// Sleep boundary used for lease renewal and empty-queue polling.
pub trait WorkerClock: Send + Sync {
    /// Suspend for a worker timer interval.
    fn sleep(&self, seconds: f64) -> BoxFuture<'static, ()>;
}

/// Production clock backed by the Tokio timer wheel.
#[derive(Clone, Copy, Debug, Default)]
pub struct TokioWorkerClock;

impl WorkerClock for TokioWorkerClock {
    fn sleep(&self, seconds: f64) -> BoxFuture<'static, ()> {
        Box::pin(tokio::time::sleep(std::time::Duration::from_secs_f64(
            seconds,
        )))
    }
}

/// Validated queue delivery controls derived from T026 settings.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct QueueWorkerPolicy {
    /// How long one received message stays invisible.
    pub visibility_timeout_seconds: u64,
    /// How often the worker renews the visibility lease.
    pub visibility_renewal_seconds: u64,
    /// Delivery attempts before dead-lettering.
    pub max_delivery_attempts: u32,
    /// Whether exhausted messages are dead-lettered.
    pub dead_letter_on_exhaustion: bool,
}

impl QueueWorkerPolicy {
    /// Validate the delivery controls.
    pub fn new(
        visibility_timeout_seconds: u64,
        visibility_renewal_seconds: u64,
        max_delivery_attempts: u32,
        dead_letter_on_exhaustion: bool,
    ) -> Result<Self, DataError> {
        if visibility_timeout_seconds < 1 {
            return Err(DataError::value("visibility timeout must be positive"));
        }
        if visibility_renewal_seconds < 1 {
            return Err(DataError::value(
                "visibility renewal interval must be positive",
            ));
        }
        if visibility_renewal_seconds >= visibility_timeout_seconds {
            return Err(DataError::value(
                "visibility renewal interval must be shorter than timeout",
            ));
        }
        if max_delivery_attempts < 1 {
            return Err(DataError::value("max delivery attempts must be positive"));
        }
        Ok(Self {
            visibility_timeout_seconds,
            visibility_renewal_seconds,
            max_delivery_attempts,
            dead_letter_on_exhaustion,
        })
    }

    /// Build worker controls from the typed T026 queue settings.
    pub fn from_settings(settings: &QueueSettings) -> Result<Self, DataError> {
        Self::new(
            settings.visibility_timeout_seconds,
            settings.visibility_renewal_seconds,
            settings.max_delivery_attempts,
            settings.dead_letter_on_exhaustion,
        )
    }
}

/// Stable result values returned by one worker delivery.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeliveryOutcome {
    /// The handler returned and the source message was deleted.
    Succeeded,
    /// The delivery failed but retries remain on the provider side.
    Retry,
    /// The delivery exhausted its attempts and was dead-lettered.
    DeadLettered,
}

impl DeliveryOutcome {
    /// The stable Python result value.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Succeeded => "succeeded",
            Self::Retry => "retry",
            Self::DeadLettered => "dead_lettered",
        }
    }
}

impl fmt::Display for DeliveryOutcome {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// Observed disposition of one received message.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeliveryResult {
    /// The provider message identifier.
    pub message_id: String,
    /// The provider delivery count for this attempt.
    pub attempt: u32,
    /// The observed disposition.
    pub outcome: DeliveryOutcome,
    /// The error text for retry and dead-letter outcomes.
    pub error: Option<String>,
}

/// Process one leased message at a time with safe at-least-once semantics.
pub struct QueueWorker {
    queue: Arc<dyn QueueTransport>,
    handler: Arc<dyn QueueHandler>,
    policy: QueueWorkerPolicy,
    dead_letter: Option<Arc<dyn DeadLetterSink>>,
    clock: Arc<dyn WorkerClock>,
    idle_sleep_seconds: f64,
}

impl QueueWorker {
    /// Build a worker over injected boundaries.
    ///
    /// A dead-letter sink is required whenever the policy dead-letters on
    /// exhaustion, and the idle sleep interval must be positive.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        queue: Arc<dyn QueueTransport>,
        handler: Arc<dyn QueueHandler>,
        policy: QueueWorkerPolicy,
        dead_letter: Option<Arc<dyn DeadLetterSink>>,
        clock: Option<Arc<dyn WorkerClock>>,
        idle_sleep_seconds: f64,
    ) -> Result<Self, DataError> {
        if policy.dead_letter_on_exhaustion && dead_letter.is_none() {
            return Err(DataError::value(
                "dead_letter sink is required when exhaustion is enabled",
            ));
        }
        if idle_sleep_seconds <= 0.0 {
            return Err(DataError::value("idle sleep interval must be positive"));
        }
        Ok(Self {
            queue,
            handler,
            policy,
            dead_letter,
            clock: clock.unwrap_or_else(|| Arc::new(TokioWorkerClock)),
            idle_sleep_seconds,
        })
    }

    /// Construct a worker directly from the typed queue settings.
    #[allow(clippy::too_many_arguments)]
    pub fn from_settings(
        queue: Arc<dyn QueueTransport>,
        handler: Arc<dyn QueueHandler>,
        settings: &QueueSettings,
        dead_letter: Option<Arc<dyn DeadLetterSink>>,
        clock: Option<Arc<dyn WorkerClock>>,
        idle_sleep_seconds: f64,
    ) -> Result<Self, DataError> {
        Self::new(
            queue,
            handler,
            QueueWorkerPolicy::from_settings(settings)?,
            dead_letter,
            clock,
            idle_sleep_seconds,
        )
    }

    /// Receive and process one message, or return `None` when the queue is empty.
    pub async fn process_one(&self) -> Result<Option<DeliveryResult>, DataError> {
        let Some(message) = self
            .queue
            .receive_one(self.policy.visibility_timeout_seconds)
            .await?
        else {
            return Ok(None);
        };
        if message.dequeue_count > self.policy.max_delivery_attempts {
            return self
                .exhausted(&message, "delivery attempt limit exceeded".to_string())
                .await
                .map(Some);
        }

        let current = Arc::new(Mutex::new(message));
        let renewal_error = Arc::new(Mutex::new(None::<DataError>));
        let renewal = self.renew_lease_task(Arc::clone(&current), Arc::clone(&renewal_error));

        let handler_error = async {
            let snapshot = lock(&current).clone();
            let payload = decode_payload(&snapshot.content)?;
            let delivery = QueueDelivery::new(snapshot, payload);
            self.handler.handle(&delivery).await
        }
        .await
        .err();

        renewal.abort();
        let _ = renewal.await;

        let error = handler_error.or_else(|| lock(&renewal_error).take());
        let current = lock(&current).clone();
        match error {
            Some(error) => Ok(Some(self.failed(&current, error_message(&error)).await?)),
            None => {
                self.queue.delete(&current).await?;
                Ok(Some(DeliveryResult {
                    message_id: current.message_id.clone(),
                    attempt: current.dequeue_count,
                    outcome: DeliveryOutcome::Succeeded,
                    error: None,
                }))
            }
        }
    }

    /// Run until stopped; `max_messages` provides a deterministic test boundary.
    ///
    /// The stop event is polled between deliveries, exactly like the Python
    /// `asyncio.Event` check at the top of the delivery loop.
    pub async fn run(
        &self,
        stop_event: Option<&AtomicBool>,
        max_messages: Option<usize>,
    ) -> Result<Vec<DeliveryResult>, DataError> {
        let mut results = Vec::new();
        while max_messages.is_none_or(|max| results.len() < max) {
            if stop_event.is_some_and(|stop| stop.load(Ordering::SeqCst)) {
                break;
            }
            match self.process_one().await? {
                None => self.clock.sleep(self.idle_sleep_seconds).await,
                Some(result) => results.push(result),
            }
        }
        Ok(results)
    }

    /// Renew the current message's lease until the handler finishes.
    ///
    /// Spawned as its own task exactly like the Python worker's renewal task:
    /// the handler is never cancelled by an early renewal exit, and a renewal
    /// failure is recorded instead of propagated, matching the Python renewal
    /// error capture, leaving the last successfully renewed lease in place.
    fn renew_lease_task(
        &self,
        current: Arc<Mutex<QueueMessage>>,
        renewal_error: Arc<Mutex<Option<DataError>>>,
    ) -> tokio::task::JoinHandle<()> {
        tokio::spawn(renew_lease(
            Arc::clone(&self.queue),
            Arc::clone(&self.clock),
            self.policy.clone(),
            current,
            renewal_error,
        ))
    }

    async fn failed(
        &self,
        message: &QueueMessage,
        error: String,
    ) -> Result<DeliveryResult, DataError> {
        if message.dequeue_count < self.policy.max_delivery_attempts {
            return Ok(DeliveryResult {
                message_id: message.message_id.clone(),
                attempt: message.dequeue_count,
                outcome: DeliveryOutcome::Retry,
                error: Some(error),
            });
        }
        self.exhausted(message, error).await
    }

    async fn exhausted(
        &self,
        message: &QueueMessage,
        error: String,
    ) -> Result<DeliveryResult, DataError> {
        if !self.policy.dead_letter_on_exhaustion {
            return Ok(DeliveryResult {
                message_id: message.message_id.clone(),
                attempt: message.dequeue_count,
                outcome: DeliveryOutcome::Retry,
                error: Some(error),
            });
        }
        let Some(dead_letter) = &self.dead_letter else {
            return Err(DataError::value(
                "dead-letter sink is required for an exhausted message",
            ));
        };
        dead_letter.send(message, &error).await?;
        self.queue.delete(message).await?;
        Ok(DeliveryResult {
            message_id: message.message_id.clone(),
            attempt: message.dequeue_count,
            outcome: DeliveryOutcome::DeadLettered,
            error: Some(error),
        })
    }
}

/// Renew the lease on a fixed cadence until the handler finishes.
///
/// Sleeps first, then renews, exactly like the Python renewal loop: a lease
/// is never renewed before the configured renewal interval has elapsed. A
/// renewal failure is recorded and the loop exits; the handler keeps running.
async fn renew_lease(
    queue: Arc<dyn QueueTransport>,
    clock: Arc<dyn WorkerClock>,
    policy: QueueWorkerPolicy,
    current: Arc<Mutex<QueueMessage>>,
    renewal_error: Arc<Mutex<Option<DataError>>>,
) {
    loop {
        clock.sleep(policy.visibility_renewal_seconds as f64).await;
        let message = lock(&current).clone();
        match queue
            .renew(&message, policy.visibility_timeout_seconds)
            .await
        {
            Ok(renewed) => {
                *lock(&current) = renewed;
            }
            Err(error) => {
                *lock(&renewal_error) = Some(error);
                return;
            }
        }
    }
}

/// Decode a message body into the JSON object handlers receive.
fn decode_payload(content: &str) -> Result<Value, DataError> {
    let decoded: Value = serde_json::from_str(content)
        .map_err(|_| DataError::value("queue message content must be valid JSON"))?;
    if !decoded.is_object() {
        return Err(DataError::value(
            "queue message content must be a JSON object",
        ));
    }
    Ok(decoded)
}

/// Format one failure the way Python's `_error_message` does.
fn error_message(error: &DataError) -> String {
    let kind = match error {
        DataError::TypeError(_) => "TypeError",
        DataError::ValueError(_) => "ValueError",
        DataError::Lookahead(_) => "LookaheadError",
        DataError::Incomplete(_) => "IncompleteError",
        DataError::Unsupported(_) => "UnsupportedError",
        DataError::EnsureJob(_) => "EnsureJobError",
        DataError::Arithmetic(_) => "ArithmeticError",
        DataError::Protocol(_) => "ProtocolError",
        DataError::NotFound(_) => "NotFoundError",
        DataError::Conflict(_) => "ConflictError",
        DataError::Io(_) => "IoError",
    };
    let detail = error.to_string().trim().to_string();
    if detail.is_empty() {
        kind.to_string()
    } else {
        format!("{kind}: {detail}")
    }
}

/// Lock a data mutex, ignoring poisoning: the guarded values are plain data
/// and a panic elsewhere must not take the renewal loop down with it.
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}
