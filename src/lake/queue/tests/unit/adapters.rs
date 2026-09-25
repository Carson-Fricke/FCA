//! Hermetic tests for the Azure queue adapters.

use std::sync::{Arc, Mutex};

use super::*;
use crate::worker::QueueTransport;
use lake_contracts::DataError;
use lake_storage::QueueSender as _;

/// Duck-typed `QueueClient` fake mirroring the Python tests' seam.
#[derive(Default)]
struct FakeQueueClient {
    received: Mutex<Vec<u64>>,
    next: Mutex<Option<AzureRawMessage>>,
    renewals: Mutex<Vec<(String, String, String, u64)>>,
    next_pop_receipt: Mutex<String>,
    deletions: Mutex<Vec<(String, String)>>,
    sent: Mutex<Vec<String>>,
}

impl FakeQueueClient {
    fn enqueue(&self, id: &str, pop_receipt: &str, content: &str, dequeue_count: u64) {
        *self
            .next
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(AzureRawMessage {
            id: id.to_string(),
            pop_receipt: pop_receipt.to_string(),
            content: content.to_string(),
            dequeue_count,
        });
    }
}

impl AzureQueueClient for FakeQueueClient {
    fn receive_one<'s>(
        &'s self,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<'s, Result<Option<AzureRawMessage>, DataError>> {
        self.received
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push(visibility_timeout_seconds);
        let message = self
            .next
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take();
        Box::pin(async move { Ok(message) })
    }

    fn update<'s>(
        &'s self,
        message_id: &'s str,
        pop_receipt: &'s str,
        content: &'s str,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<'s, Result<String, DataError>> {
        self.renewals
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push((
                message_id.to_string(),
                pop_receipt.to_string(),
                content.to_string(),
                visibility_timeout_seconds,
            ));
        let new_receipt = self
            .next_pop_receipt
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        Box::pin(async move { Ok(new_receipt) })
    }

    fn delete<'s>(
        &'s self,
        message_id: &'s str,
        pop_receipt: &'s str,
    ) -> BoxFuture<'s, Result<(), DataError>> {
        self.deletions
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push((message_id.to_string(), pop_receipt.to_string()));
        Box::pin(async move { Ok(()) })
    }

    fn send<'s>(&'s self, content: String) -> BoxFuture<'s, Result<(), DataError>> {
        self.sent
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push(content);
        Box::pin(async move { Ok(()) })
    }
}

fn message(id: &str, pop_receipt: &str, content: &str, dequeue_count: u32) -> QueueMessage {
    QueueMessage::new(id, pop_receipt, content, dequeue_count).expect("queue message")
}

#[tokio::test]
async fn transport_receives_one_message_per_call_and_maps_fields() {
    let client = Arc::new(FakeQueueClient::default());
    client.enqueue("m-1", "receipt-1", r#"{"job_id":"job-1"}"#, 2);
    let transport = AzureQueueTransport::new(Arc::clone(&client) as Arc<dyn AzureQueueClient>);

    let received = transport.receive_one(30).await.expect("receive");

    assert_eq!(
        received,
        Some(message("m-1", "receipt-1", r#"{"job_id":"job-1"}"#, 2))
    );
    assert_eq!(
        client
            .received
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone(),
        vec![30]
    );

    // The empty queue returns `None`.
    assert_eq!(transport.receive_one(30).await.expect("receive"), None);
}

#[tokio::test]
async fn transport_rejects_non_positive_delivery_counts() {
    let client = Arc::new(FakeQueueClient::default());
    client.enqueue("m-1", "receipt-1", "content", 0);
    let transport = AzureQueueTransport::new(Arc::clone(&client) as Arc<dyn AzureQueueClient>);

    let error = transport.receive_one(30).await.expect_err("must reject");
    assert!(matches!(error, DataError::ValueError(_)));
}

#[tokio::test]
async fn transport_renews_visibility_and_maps_the_new_pop_receipt() {
    let client = Arc::new(FakeQueueClient::default());
    *client
        .next_pop_receipt
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner()) = "receipt-2".to_string();
    let transport = AzureQueueTransport::new(Arc::clone(&client) as Arc<dyn AzureQueueClient>);
    let original = message("m-1", "receipt-1", "content", 1);

    let renewed = transport.renew(&original, 45).await.expect("renew");

    assert_eq!(
        renewed,
        message("m-1", "receipt-2", "content", 1),
        "content and dequeue count must be preserved while the receipt updates"
    );
    assert_eq!(
        client
            .renewals
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone(),
        vec![(
            "m-1".to_string(),
            "receipt-1".to_string(),
            "content".to_string(),
            45
        )]
    );
}

#[tokio::test]
async fn transport_deletes_by_message_id_and_pop_receipt() {
    let client = Arc::new(FakeQueueClient::default());
    let transport = AzureQueueTransport::new(Arc::clone(&client) as Arc<dyn AzureQueueClient>);
    let message = message("m-1", "receipt-1", "content", 1);

    transport.delete(&message).await.expect("delete");

    assert_eq!(
        client
            .deletions
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone(),
        vec![("m-1".to_string(), "receipt-1".to_string())]
    );
}

#[tokio::test]
async fn sender_serializes_deterministic_json_and_only_sends() {
    let client = Arc::new(FakeQueueClient::default());
    let sender = AzureQueueSender::new(Arc::clone(&client) as Arc<dyn AzureQueueClient>);

    let payload: Value =
        serde_json::from_str(r#"{"symbol":"AAPL","session_date":"2024-01-19"}"#).expect("payload");
    sender.send(&payload).await.expect("send");

    assert_eq!(
        client
            .sent
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone(),
        vec![r#"{"session_date":"2024-01-19","symbol":"AAPL"}"#.to_string()]
    );
}

#[tokio::test]
async fn sender_rejects_non_object_messages() {
    let client = Arc::new(FakeQueueClient::default());
    let sender = AzureQueueSender::new(Arc::clone(&client) as Arc<dyn AzureQueueClient>);

    let error = sender.send(&Value::from(7)).await.expect_err("must reject");
    assert!(matches!(error, DataError::ValueError(_)));
    assert!(client
        .sent
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .is_empty());
}

#[test]
fn canonical_json_sorts_keys_and_escapes_non_ascii() {
    let payload: Value = serde_json::from_str(r#"{"b":2,"a":"α","nested":{"z":true,"a":[1,"β"]}}"#)
        .expect("payload");
    assert_eq!(
        canonical_json(&payload),
        r#"{"a":"\u03B1","b":2,"nested":{"a":[1,"\u03B2"],"z":true}}"#
    );
}
