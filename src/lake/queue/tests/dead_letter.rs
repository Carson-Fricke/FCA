//! Parity tests for dead-letter envelopes and replay, ported from
//! `tests/lake/queue/test_dead_letter.py`.

use std::sync::{Arc, Mutex};

use futures_util::future::BoxFuture;
use lake_contracts::{DataError, DateTime};
use lake_queue::{
    DeadLetterEnvelope, DeadLetterReplayer, DeadLetterStore, DeliveryOutcome, QueueDeadLetterSink,
    QueueMessage, QueueTransport, QueueWorker, QueueWorkerPolicy, DEAD_LETTER_SCHEMA_VERSION,
};
use lake_storage::QueueSender;
use serde_json::{json, Value};

#[derive(Default)]
struct FakeSender {
    messages: Mutex<Vec<Value>>,
}

impl FakeSender {
    fn messages(&self) -> Vec<Value> {
        self.messages.lock().unwrap().clone()
    }
}

impl QueueSender for FakeSender {
    fn send<'a>(&'a self, message: &'a Value) -> BoxFuture<'a, Result<(), DataError>> {
        Box::pin(async move {
            self.messages.lock().unwrap().push(message.clone());
            Ok(())
        })
    }
}

#[derive(Default)]
struct FakeQueue {
    messages: Mutex<Vec<QueueMessage>>,
    deleted: Mutex<Vec<QueueMessage>>,
}

impl FakeQueue {
    fn new(messages: Vec<QueueMessage>) -> Self {
        Self {
            messages: Mutex::new(messages),
            deleted: Mutex::new(Vec::new()),
        }
    }

    fn deleted(&self) -> Vec<QueueMessage> {
        self.deleted.lock().unwrap().clone()
    }
}

impl QueueTransport for FakeQueue {
    fn receive_one<'a>(
        &'a self,
        _visibility_timeout_seconds: u64,
    ) -> BoxFuture<'a, Result<Option<QueueMessage>, DataError>> {
        Box::pin(async move {
            let mut messages = self.messages.lock().unwrap();
            if messages.is_empty() {
                return Ok(None);
            }
            Ok(Some(messages.remove(0)))
        })
    }

    fn renew<'a>(
        &'a self,
        message: &'a QueueMessage,
        _visibility_timeout_seconds: u64,
    ) -> BoxFuture<'a, Result<QueueMessage, DataError>> {
        Box::pin(async move { Ok(message.clone()) })
    }

    fn delete<'a>(&'a self, message: &'a QueueMessage) -> BoxFuture<'a, Result<(), DataError>> {
        Box::pin(async move {
            self.deleted.lock().unwrap().push(message.clone());
            Ok(())
        })
    }
}

struct RecordingHandler;

impl lake_queue::QueueHandler for RecordingHandler {
    fn handle<'a>(
        &'a self,
        _delivery: &'a lake_queue::QueueDelivery,
    ) -> BoxFuture<'a, Result<(), DataError>> {
        Box::pin(async { Err(DataError::value("permanent source contract failure")) })
    }
}

fn message(attempt: u32) -> QueueMessage {
    message_with(attempt, r#"{"job_id":"job-1"}"#, "message-1")
}

fn message_with(attempt: u32, content: &str, message_id: &str) -> QueueMessage {
    QueueMessage::new(message_id, format!("receipt-{attempt}"), content, attempt).expect("message")
}

fn policy() -> QueueWorkerPolicy {
    QueueWorkerPolicy::new(30, 5, 2, true).expect("policy")
}

fn fixed_instant() -> DateTime {
    DateTime::from_ymd_hms(2026, 9, 1, 23, 0, 0, 0).expect("instant")
}

#[test]
fn envelope_preserves_failure_metadata_and_round_trips() {
    let envelope = DeadLetterEnvelope::from_failure(
        &message(3),
        "equity-backfill",
        "ValueError: provider response omitted close",
        fixed_instant(),
    )
    .expect("envelope");

    let encoded = envelope.as_message();
    let decoded = DeadLetterEnvelope::from_message(&encoded).expect("decoded");

    assert_eq!(decoded, envelope);
    assert_eq!(encoded["schema_version"], DEAD_LETTER_SCHEMA_VERSION);
    assert_eq!(encoded["error_type"], "ValueError");
    assert_eq!(encoded["error_message"], "provider response omitted close");
    assert_eq!(encoded["source_message_id"], "message-1");
    assert_eq!(encoded["original_content"], r#"{"job_id":"job-1"}"#);
    assert_eq!(envelope.dead_letter_id.len(), 64);
}

#[tokio::test]
async fn queue_worker_stops_after_limit_and_sends_before_source_delete() {
    let queue = Arc::new(FakeQueue::new(vec![message(1), message(2)]));
    let sender = Arc::new(FakeSender::default());
    let store = Arc::new(lake_queue::InMemoryDeadLetterStore::new());
    let sink = QueueDeadLetterSink::with_parts(
        sender.clone(),
        "equity-backfill",
        Some(Arc::new(fixed_instant)),
        Some(store.clone()),
    )
    .expect("sink");
    let worker = QueueWorker::new(
        queue.clone(),
        Arc::new(RecordingHandler),
        policy(),
        Some(Arc::new(sink)),
        None,
        1.0,
    )
    .expect("worker");

    let results = worker.run(None, Some(2)).await.expect("run");

    let outcomes: Vec<DeliveryOutcome> = results.iter().map(|result| result.outcome).collect();
    assert_eq!(
        outcomes,
        vec![DeliveryOutcome::Retry, DeliveryOutcome::DeadLettered]
    );
    let sent = sender.messages();
    assert_eq!(sent.len(), 1);
    assert_eq!(store.list().len(), 1);
    assert_eq!(queue.deleted(), vec![message(2)]);
    let original = sent[0]["original_content"]
        .as_str()
        .expect("original content");
    assert_eq!(
        serde_json::from_str::<Value>(original).expect("decoded original"),
        json!({"job_id": "job-1"})
    );
}

#[tokio::test]
async fn replay_sends_original_payload_and_supports_repaired_payload() {
    let sender = Arc::new(FakeSender::default());
    let envelope = DeadLetterEnvelope::from_failure(
        &message_with(1, r#"{"job_id":"job-1","bad":true}"#, "message-1"),
        "equity-backfill",
        "ValueError: bad source response",
        fixed_instant(),
    )
    .expect("envelope");
    let replayer = DeadLetterReplayer::new(sender.clone(), "equity-backfill").expect("replayer");

    let result = replayer.replay(&envelope, None).await.expect("replay");
    let repaired_envelope = DeadLetterEnvelope {
        original_content: "not-json".to_string(),
        ..envelope.clone()
    };
    let repaired = replayer
        .replay(
            &repaired_envelope,
            Some(&json!({"job_id": "job-1", "repair": "drop-bad-field"})),
        )
        .await
        .expect("repaired replay");

    assert_eq!(
        result,
        lake_queue::ReplayResult {
            dead_letter_id: envelope.dead_letter_id.clone(),
            source_queue: "equity-backfill".to_string(),
            source_message_id: "message-1".to_string(),
            repaired_payload: false,
        }
    );
    assert!(repaired.repaired_payload);
    assert_eq!(
        sender.messages(),
        vec![
            json!({"job_id": "job-1", "bad": true}),
            json!({"job_id": "job-1", "repair": "drop-bad-field"}),
        ]
    );
}

#[tokio::test]
async fn replay_rejects_invalid_original_and_wrong_target() {
    let envelope = DeadLetterEnvelope::from_failure(
        &message_with(1, "not-json", "message-1"),
        "equity-backfill",
        "ValueError: invalid payload",
        fixed_instant(),
    )
    .expect("envelope");

    let error = DeadLetterReplayer::new(Arc::new(FakeSender::default()), "equity-backfill")
        .expect("replayer")
        .replay(&envelope, None)
        .await
        .expect_err("invalid original");
    assert!(error.to_string().contains("supply a repaired payload"));

    let error = DeadLetterReplayer::new(Arc::new(FakeSender::default()), "options-backfill")
        .expect("replayer")
        .replay(&envelope, Some(&json!({"job_id": "job-1"})))
        .await
        .expect_err("wrong target");
    assert!(error.to_string().contains("does not match"));
}

#[test]
fn store_deduplicates_stable_id_and_orders_by_failure_time() {
    let first = DeadLetterEnvelope::from_failure(
        &message_with(1, r#"{"job_id":"job-1"}"#, "first"),
        "equity-backfill",
        "ValueError: first",
        DateTime::from_ymd_hms(2026, 9, 1, 23, 2, 0, 0).expect("instant"),
    )
    .expect("first");
    let second = DeadLetterEnvelope::from_failure(
        &message_with(1, r#"{"job_id":"job-1"}"#, "second"),
        "equity-backfill",
        "ValueError: second",
        DateTime::from_ymd_hms(2026, 9, 1, 23, 1, 0, 0).expect("instant"),
    )
    .expect("second");
    let store =
        lake_queue::InMemoryDeadLetterStore::with_envelopes([first.clone(), second.clone()]);

    let replacement = DeadLetterEnvelope {
        error: "ValueError: updated".to_string(),
        ..first.clone()
    };
    assert_eq!(store.put(replacement.clone()), replacement);
    assert_eq!(store.get(&first.dead_letter_id), Some(replacement));
    let listed: Vec<String> = store
        .list()
        .iter()
        .map(|envelope| envelope.dead_letter_id.clone())
        .collect();
    assert_eq!(
        listed,
        vec![second.dead_letter_id.clone(), first.dead_letter_id.clone()]
    );
}
