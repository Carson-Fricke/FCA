//! Parity tests for the queue worker, ported from `tests/lake/queue/test_worker.py`.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

use futures_util::future::BoxFuture;
use lake_configuration::QueueSettings;
use lake_contracts::DataError;
use lake_queue::{
    DeadLetterSink, DeliveryOutcome, DeliveryResult, QueueDelivery, QueueHandler, QueueMessage,
    QueueTransport, QueueWorker, QueueWorkerPolicy, WorkerClock,
};
use serde_json::json;

#[derive(Clone, Default)]
struct FakeClock {
    sleeps: Arc<Mutex<Vec<f64>>>,
    renewal_ready: Arc<tokio::sync::Notify>,
}

impl FakeClock {
    fn new() -> Self {
        Self::default()
    }
}

impl WorkerClock for FakeClock {
    fn sleep(&self, seconds: f64) -> BoxFuture<'static, ()> {
        let this = self.clone();
        Box::pin(async move {
            this.sleeps.lock().unwrap().push(seconds);
            if seconds == 2.0 {
                this.renewal_ready.notified().await;
            }
        })
    }
}

#[derive(Default)]
struct FakeQueue {
    messages: Mutex<Vec<QueueMessage>>,
    received: Mutex<Vec<(QueueMessage, u64)>>,
    renewals: Mutex<Vec<(QueueMessage, u64)>>,
    deleted: Mutex<Vec<QueueMessage>>,
}

impl FakeQueue {
    fn new(messages: Vec<QueueMessage>) -> Self {
        Self {
            messages: Mutex::new(messages),
            received: Mutex::new(Vec::new()),
            renewals: Mutex::new(Vec::new()),
            deleted: Mutex::new(Vec::new()),
        }
    }

    fn received(&self) -> Vec<(QueueMessage, u64)> {
        self.received.lock().unwrap().clone()
    }

    fn renewals(&self) -> Vec<(QueueMessage, u64)> {
        self.renewals.lock().unwrap().clone()
    }

    fn deleted(&self) -> Vec<QueueMessage> {
        self.deleted.lock().unwrap().clone()
    }
}

impl QueueTransport for FakeQueue {
    fn receive_one<'a>(
        &'a self,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<'a, Result<Option<QueueMessage>, DataError>> {
        Box::pin(async move {
            let mut messages = self.messages.lock().unwrap();
            if messages.is_empty() {
                return Ok(None);
            }
            let message = messages.remove(0);
            self.received
                .lock()
                .unwrap()
                .push((message.clone(), visibility_timeout_seconds));
            Ok(Some(message))
        })
    }

    fn renew<'a>(
        &'a self,
        message: &'a QueueMessage,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<'a, Result<QueueMessage, DataError>> {
        Box::pin(async move {
            self.renewals
                .lock()
                .unwrap()
                .push((message.clone(), visibility_timeout_seconds));
            Ok(QueueMessage {
                pop_receipt: format!("{}-renewed", message.pop_receipt),
                ..message.clone()
            })
        })
    }

    fn delete<'a>(&'a self, message: &'a QueueMessage) -> BoxFuture<'a, Result<(), DataError>> {
        Box::pin(async move {
            self.deleted.lock().unwrap().push(message.clone());
            Ok(())
        })
    }
}

#[derive(Default)]
struct FakeDeadLetter {
    sent: Mutex<Vec<(QueueMessage, String)>>,
}

impl FakeDeadLetter {
    fn sent(&self) -> Vec<(QueueMessage, String)> {
        self.sent.lock().unwrap().clone()
    }
}

impl DeadLetterSink for FakeDeadLetter {
    fn send<'a>(
        &'a self,
        message: &'a QueueMessage,
        error: &'a str,
    ) -> BoxFuture<'a, Result<(), DataError>> {
        Box::pin(async move {
            self.sent
                .lock()
                .unwrap()
                .push((message.clone(), error.to_string()));
            Ok(())
        })
    }
}

/// Record deliveries, optionally failing every one like the Python handlers.
struct RecordingHandler {
    deliveries: Mutex<Vec<QueueDelivery>>,
    error: Option<DataError>,
}

impl RecordingHandler {
    fn recording() -> Arc<Self> {
        Arc::new(Self {
            deliveries: Mutex::new(Vec::new()),
            error: None,
        })
    }

    fn failing(message: &str) -> Arc<Self> {
        Arc::new(Self {
            deliveries: Mutex::new(Vec::new()),
            error: Some(DataError::value(message)),
        })
    }

    fn deliveries(&self) -> Vec<QueueDelivery> {
        self.deliveries.lock().unwrap().clone()
    }
}

impl QueueHandler for RecordingHandler {
    fn handle<'a>(&'a self, delivery: &'a QueueDelivery) -> BoxFuture<'a, Result<(), DataError>> {
        Box::pin(async move {
            self.deliveries.lock().unwrap().push(delivery.clone());
            match &self.error {
                Some(error) => Err(error.clone()),
                None => Ok(()),
            }
        })
    }
}

/// Handler that blocks until the test releases it, for renewal coordination.
struct BlockingHandler {
    started: tokio::sync::mpsc::UnboundedSender<()>,
    release: Arc<tokio::sync::Notify>,
}

impl QueueHandler for BlockingHandler {
    fn handle<'a>(&'a self, _delivery: &'a QueueDelivery) -> BoxFuture<'a, Result<(), DataError>> {
        Box::pin(async move {
            let _ = self.started.send(());
            self.release.notified().await;
            Ok(())
        })
    }
}

/// Idempotent handler keyed by `job_id`, ported from the duplicate-delivery test.
struct IdempotentHandler {
    objects: Arc<Mutex<BTreeMap<String, String>>>,
    catalog: Arc<Mutex<BTreeSet<String>>>,
}

impl QueueHandler for IdempotentHandler {
    fn handle<'a>(&'a self, delivery: &'a QueueDelivery) -> BoxFuture<'a, Result<(), DataError>> {
        Box::pin(async move {
            let job_id = delivery.payload()["job_id"]
                .as_str()
                .expect("job id")
                .to_string();
            self.objects
                .lock()
                .unwrap()
                .insert(job_id.clone(), "published".to_string());
            self.catalog.lock().unwrap().insert(job_id);
            Ok(())
        })
    }
}

fn message(attempt: u32) -> QueueMessage {
    QueueMessage::new(
        "message-1",
        format!("receipt-{attempt}"),
        r#"{"job_id":"job-1"}"#,
        attempt,
    )
    .expect("message")
}

fn policy(attempts: u32, dead_letter: bool) -> QueueWorkerPolicy {
    QueueWorkerPolicy::new(10, 2, attempts, dead_letter).expect("policy")
}

fn succeeded(message_id: &str, attempt: u32) -> DeliveryResult {
    DeliveryResult {
        message_id: message_id.to_string(),
        attempt,
        outcome: DeliveryOutcome::Succeeded,
        error: None,
    }
}

async fn wait_until(condition: impl Fn() -> bool) {
    for _ in 0..10_000 {
        if condition() {
            return;
        }
        tokio::task::yield_now().await;
    }
    panic!("condition was not reached");
}

#[test]
fn policy_is_derived_from_t026_settings_and_renewal_is_before_timeout() {
    let settings = QueueSettings {
        visibility_timeout_seconds: 300,
        visibility_renewal_seconds: 60,
        max_delivery_attempts: 5,
        dead_letter_on_exhaustion: true,
    };

    let derived = QueueWorkerPolicy::from_settings(&settings).expect("policy");
    assert_eq!(
        derived,
        QueueWorkerPolicy::new(300, 60, 5, true).expect("policy")
    );
    let error = QueueWorkerPolicy::new(10, 10, 3, true).expect_err("renewal cadence");
    assert!(error.to_string().contains("shorter than timeout"));
}

#[tokio::test]
async fn success_decodes_payload_and_deletes_only_after_handler_returns() {
    let queue = Arc::new(FakeQueue::new(vec![message(1)]));
    let handler = RecordingHandler::recording();
    let worker = QueueWorker::new(
        queue.clone(),
        handler.clone(),
        policy(3, true),
        Some(Arc::new(FakeDeadLetter::default())),
        None,
        1.0,
    )
    .expect("worker");

    let result = worker
        .process_one()
        .await
        .expect("delivery")
        .expect("result");

    assert_eq!(result, succeeded("message-1", 1));
    let deliveries = handler.deliveries();
    assert_eq!(deliveries[0].payload(), &json!({"job_id": "job-1"}));
    assert_eq!(deliveries[0].message_id(), "message-1");
    assert_eq!(deliveries[0].attempt(), 1);
    assert_eq!(queue.deleted(), vec![message(1)]);
}

#[tokio::test]
async fn long_handler_renews_visibility_and_deletes_with_new_pop_receipt() {
    let queue = Arc::new(FakeQueue::new(vec![message(1)]));
    let clock = FakeClock::new();
    let (started_tx, mut started_rx) = tokio::sync::mpsc::unbounded_channel();
    let release = Arc::new(tokio::sync::Notify::new());
    let handler = Arc::new(BlockingHandler {
        started: started_tx,
        release: Arc::clone(&release),
    });
    let worker = QueueWorker::new(
        queue.clone(),
        handler.clone(),
        policy(3, true),
        Some(Arc::new(FakeDeadLetter::default())),
        Some(Arc::new(clock.clone())),
        1.0,
    )
    .expect("worker");

    let task = tokio::spawn(async move { worker.process_one().await });
    started_rx.recv().await.expect("handler started");
    tokio::task::yield_now().await;
    assert!(queue.renewals().is_empty());
    clock.renewal_ready.notify_waiters();
    wait_until(|| queue.renewals().len() == 1).await;
    release.notify_waiters();
    let result = task
        .await
        .expect("joined")
        .expect("delivery")
        .expect("result");

    assert_eq!(result.outcome, DeliveryOutcome::Succeeded);
    assert_eq!(queue.received(), vec![(message(1), 10)]);
    assert_eq!(queue.renewals(), vec![(message(1), 10)]);
    assert_eq!(
        queue.deleted(),
        vec![QueueMessage {
            pop_receipt: "receipt-1-renewed".to_string(),
            ..message(1)
        }]
    );
}

#[tokio::test]
async fn failed_handler_is_left_for_retry_before_attempt_limit() {
    let queue = Arc::new(FakeQueue::new(vec![message(1)]));
    let dead_letter = Arc::new(FakeDeadLetter::default());
    let worker = QueueWorker::new(
        queue.clone(),
        RecordingHandler::failing("provider unavailable"),
        policy(3, true),
        Some(dead_letter.clone()),
        None,
        1.0,
    )
    .expect("worker");

    let result = worker
        .process_one()
        .await
        .expect("delivery")
        .expect("result");

    assert_eq!(
        result,
        DeliveryResult {
            message_id: "message-1".to_string(),
            attempt: 1,
            outcome: DeliveryOutcome::Retry,
            error: Some("ValueError: provider unavailable".to_string()),
        }
    );
    assert!(queue.deleted().is_empty());
    assert!(dead_letter.sent().is_empty());
}

#[tokio::test]
async fn final_failed_attempt_is_dead_lettered_before_source_delete() {
    let queue = Arc::new(FakeQueue::new(vec![message(3)]));
    let dead_letter = Arc::new(FakeDeadLetter::default());
    let worker = QueueWorker::new(
        queue.clone(),
        RecordingHandler::failing("bad source response"),
        policy(3, true),
        Some(dead_letter.clone()),
        None,
        1.0,
    )
    .expect("worker");

    let result = worker
        .process_one()
        .await
        .expect("delivery")
        .expect("result");

    assert_eq!(result.outcome, DeliveryOutcome::DeadLettered);
    assert_eq!(
        dead_letter.sent(),
        vec![(message(3), "ValueError: bad source response".to_string())]
    );
    assert_eq!(queue.deleted(), vec![message(3)]);
}

#[tokio::test]
async fn disabled_dead_letter_policy_leaves_exhausted_message_for_boundary_recovery() {
    let queue = Arc::new(FakeQueue::new(vec![message(2)]));
    let worker = QueueWorker::new(
        queue.clone(),
        RecordingHandler::failing("still unavailable"),
        policy(2, false),
        None,
        None,
        1.0,
    )
    .expect("worker");

    let result = worker
        .process_one()
        .await
        .expect("delivery")
        .expect("result");

    assert_eq!(result.outcome, DeliveryOutcome::Retry);
    assert!(queue.deleted().is_empty());
}

#[tokio::test]
async fn delivery_past_attempt_limit_is_not_handled_again() {
    let queue = Arc::new(FakeQueue::new(vec![message(4)]));
    let handler = RecordingHandler::recording();
    let dead_letter = Arc::new(FakeDeadLetter::default());
    let worker = QueueWorker::new(
        queue.clone(),
        handler.clone(),
        policy(3, true),
        Some(dead_letter.clone()),
        None,
        1.0,
    )
    .expect("worker");

    let result = worker
        .process_one()
        .await
        .expect("delivery")
        .expect("result");

    assert_eq!(result.outcome, DeliveryOutcome::DeadLettered);
    assert!(handler.deliveries().is_empty());
    assert_eq!(
        dead_letter.sent(),
        vec![(message(4), "delivery attempt limit exceeded".to_string())]
    );
    assert_eq!(queue.deleted(), vec![message(4)]);
}

#[tokio::test]
async fn duplicate_delivery_uses_idempotent_handler_and_deterministic_result() {
    let first = message(1);
    let duplicate =
        QueueMessage::new("message-2", "receipt-2", r#"{"job_id":"job-1"}"#, 1).expect("duplicate");
    let queue = Arc::new(FakeQueue::new(vec![first.clone(), duplicate.clone()]));
    let objects = Arc::new(Mutex::new(BTreeMap::new()));
    let catalog = Arc::new(Mutex::new(BTreeSet::new()));
    let worker = QueueWorker::new(
        queue.clone(),
        Arc::new(IdempotentHandler {
            objects: Arc::clone(&objects),
            catalog: Arc::clone(&catalog),
        }),
        policy(3, true),
        Some(Arc::new(FakeDeadLetter::default())),
        None,
        1.0,
    )
    .expect("worker");

    let results = worker.run(None, Some(2)).await.expect("run");

    assert_eq!(
        results
            .iter()
            .map(|result| result.outcome)
            .collect::<Vec<_>>(),
        vec![DeliveryOutcome::Succeeded, DeliveryOutcome::Succeeded]
    );
    assert_eq!(
        objects.lock().unwrap().clone(),
        BTreeMap::from([("job-1".to_string(), "published".to_string())])
    );
    assert_eq!(
        catalog.lock().unwrap().clone(),
        BTreeSet::from(["job-1".to_string()])
    );
    assert_eq!(queue.deleted(), vec![first, duplicate]);
}

#[tokio::test]
async fn worker_loop_stops_after_requested_deliveries() {
    let second = QueueMessage::new("message-2", "receipt-1", r#"{"job_id":"job-1"}"#, 1)
        .expect("second message");
    let queue = Arc::new(FakeQueue::new(vec![message(1), second]));
    let handler = RecordingHandler::recording();
    let worker = QueueWorker::new(
        queue.clone(),
        handler.clone(),
        policy(3, true),
        Some(Arc::new(FakeDeadLetter::default())),
        None,
        1.0,
    )
    .expect("worker");

    let results = worker.run(None, Some(2)).await.expect("run");

    assert_eq!(results.len(), 2);
    let handled: Vec<String> = handler
        .deliveries()
        .iter()
        .map(|delivery| delivery.message_id().to_string())
        .collect();
    assert_eq!(
        handled,
        vec!["message-1".to_string(), "message-2".to_string()]
    );
}

#[test]
fn exhaustion_requires_a_dead_letter_boundary() {
    let queue = Arc::new(FakeQueue::new(Vec::new()));
    let error = QueueWorker::new(
        queue,
        RecordingHandler::recording(),
        policy(3, true),
        None,
        None,
        1.0,
    )
    .err()
    .expect("boundary");
    assert!(error
        .to_string()
        .contains("dead_letter sink is required when exhaustion is enabled"));
}
