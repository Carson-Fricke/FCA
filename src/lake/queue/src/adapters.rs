//! Azure adapters for the provider-independent queue worker.
//!
//! Ports `lake.queue.adapters`: the lease-aware [`AzureQueueTransport`] that
//! implements [`QueueTransport`] (one message per receive, `id`/`pop_receipt`
//! mapping), the [`AzureQueueSender`] that satisfies the
//! [`lake_storage::QueueSender`] boundary with deterministic JSON bodies, and
//! the SDK seam that keeps both hermetically testable. The provider-independent
//! [`crate::QueueDeadLetterSink`] covers dead-letter transfer over any sender,
//! so the Python `AzureDeadLetterSink` composition reduces to
//! `QueueDeadLetterSink::new(sender, queue_name)`.

use std::sync::Arc;
use std::time::Duration;

use azure_storage::StorageCredentials;
use azure_storage_queues::prelude::{
    NumberOfMessages, PopReceipt, QueueClient, QueueServiceClient, VisibilityTimeout,
};
use futures_util::future::BoxFuture;
use lake_contracts::DataError;
use serde_json::Value;

use crate::worker::QueueMessage;
use crate::worker::QueueTransport;

/// One raw Azure queue message before the port mapping.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AzureRawMessage {
    /// The provider message identifier (`id`).
    pub id: String,
    /// The provider lease receipt for the current receive.
    pub pop_receipt: String,
    /// The raw message body.
    pub content: String,
    /// The provider delivery count, starting at one.
    pub dequeue_count: u64,
}

/// The queue-client surface the Azure adapters are built over.
///
/// Mirrors the `QueueClient` members the Python transport duck-types:
/// receive (one message per page), visibility-preserving update, delete, and
/// raw-content send.
pub trait AzureQueueClient: Send + Sync {
    /// Receive at most one message and make it invisible.
    fn receive_one<'s>(
        &'s self,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<'s, Result<Option<AzureRawMessage>, DataError>>;

    /// Update the visibility lease, preserving content, and return the new
    /// pop receipt.
    fn update<'s>(
        &'s self,
        message_id: &'s str,
        pop_receipt: &'s str,
        content: &'s str,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<'s, Result<String, DataError>>;

    /// Delete one message by id and pop receipt.
    fn delete<'s>(
        &'s self,
        message_id: &'s str,
        pop_receipt: &'s str,
    ) -> BoxFuture<'s, Result<(), DataError>>;

    /// Send one raw message body.
    fn send<'s>(&'s self, content: String) -> BoxFuture<'s, Result<(), DataError>>;
}

/// Map one provider message into the validated [`QueueMessage`] port shape.
fn message_from_azure(raw: AzureRawMessage) -> Result<QueueMessage, DataError> {
    let dequeue_count = u32::try_from(raw.dequeue_count)
        .map_err(|_| DataError::value("dequeue_count must fit in 32 bits"))?;
    QueueMessage::new(raw.id, raw.pop_receipt, raw.content, dequeue_count)
}

/// Lease-aware Azure Storage Queue transport for the queue worker.
pub struct AzureQueueTransport {
    queue: Arc<dyn AzureQueueClient>,
}

impl AzureQueueTransport {
    /// Build the transport over an injected queue client.
    pub fn new(queue: Arc<dyn AzureQueueClient>) -> Self {
        Self { queue }
    }

    /// Build the transport from an account URL with a caller credential.
    pub fn from_account_url(
        account_url: &str,
        queue_name: &str,
        credential: Arc<dyn azure_core::auth::TokenCredential>,
    ) -> Result<Self, DataError> {
        Ok(Self {
            queue: Arc::new(SdkQueueClient::from_account_url(
                account_url,
                queue_name,
                credential,
            )?),
        })
    }
}

impl QueueTransport for AzureQueueTransport {
    fn receive_one<'a>(
        &'a self,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<'a, Result<Option<QueueMessage>, DataError>> {
        Box::pin(async move {
            self.queue
                .receive_one(visibility_timeout_seconds)
                .await?
                .map(message_from_azure)
                .transpose()
        })
    }

    fn renew<'a>(
        &'a self,
        message: &'a QueueMessage,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<'a, Result<QueueMessage, DataError>> {
        Box::pin(async move {
            // The Rust SDK 0.21 update builder always writes a message body,
            // so the content is passed through unchanged to preserve it,
            // exactly like the Python update_message call without content.
            let pop_receipt = self
                .queue
                .update(
                    &message.message_id,
                    &message.pop_receipt,
                    &message.content,
                    visibility_timeout_seconds,
                )
                .await?;
            QueueMessage::new(
                message.message_id.clone(),
                pop_receipt,
                message.content.clone(),
                message.dequeue_count,
            )
        })
    }

    fn delete<'a>(&'a self, message: &'a QueueMessage) -> BoxFuture<'a, Result<(), DataError>> {
        self.queue.delete(&message.message_id, &message.pop_receipt)
    }
}

/// JSON message sender backed by one Azure Storage Queue.
pub struct AzureQueueSender {
    queue: Arc<dyn AzureQueueClient>,
}

impl AzureQueueSender {
    /// Build the sender over an injected queue client.
    pub fn new(queue: Arc<dyn AzureQueueClient>) -> Self {
        Self { queue }
    }

    /// Build the sender from an account URL with a caller credential.
    pub fn from_account_url(
        account_url: &str,
        queue_name: &str,
        credential: Arc<dyn azure_core::auth::TokenCredential>,
    ) -> Result<Self, DataError> {
        Ok(Self {
            queue: Arc::new(SdkQueueClient::from_account_url(
                account_url,
                queue_name,
                credential,
            )?),
        })
    }
}

impl lake_storage::QueueSender for AzureQueueSender {
    fn send<'a>(&'a self, message: &'a Value) -> BoxFuture<'a, Result<(), DataError>> {
        if !message.is_object() {
            return Box::pin(async {
                Err(DataError::value("queue messages must be JSON objects"))
            });
        }
        let content = canonical_json(message);
        self.queue.send(content)
    }
}

/// Serialize a JSON object deterministically, like the Python sender:
/// sorted keys, compact separators, and ASCII-only output.
pub fn canonical_json(message: &Value) -> String {
    fn write_value(out: &mut String, value: &Value) {
        match value {
            Value::Object(object) => {
                let sorted: std::collections::BTreeMap<&String, &Value> = object.iter().collect();
                out.push('{');
                for (index, (key, value)) in sorted.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    write_string(out, key);
                    out.push(':');
                    write_value(out, value);
                }
                out.push('}');
            }
            Value::Array(items) => {
                out.push('[');
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    write_value(out, item);
                }
                out.push(']');
            }
            Value::String(text) => write_string(out, text),
            other => out.push_str(&other.to_string()),
        }
    }

    fn write_string(out: &mut String, text: &str) {
        let escaped = serde_json::to_string(text).expect("string serializes");
        for unit in escaped.encode_utf16() {
            if unit < 0x80 {
                out.push(unit as u8 as char);
            } else {
                out.push_str(&format!("\\u{unit:04X}"));
            }
        }
    }

    let mut out = String::new();
    write_value(&mut out, message);
    out
}

/// SDK-backed queue client over `azure_storage_queues`.
struct SdkQueueClient {
    queue: QueueClient,
}

impl SdkQueueClient {
    fn from_account_url(
        account_url: &str,
        queue_name: &str,
        credential: Arc<dyn azure_core::auth::TokenCredential>,
    ) -> Result<Self, DataError> {
        let trimmed = account_url.trim().trim_end_matches('/');
        let rest = trimmed
            .split_once("://")
            .map(|(_, rest)| rest)
            .unwrap_or(trimmed);
        let host = rest
            .split(['/', '?', '#'])
            .next()
            .unwrap_or_default()
            .to_string();
        let account = host
            .strip_suffix(".queue.core.windows.net")
            .unwrap_or(&host)
            .to_string();
        if account.is_empty() {
            return Err(DataError::value(
                "queue service URL must contain an account name",
            ));
        }
        let credentials = StorageCredentials::token_credential(credential);
        Ok(Self {
            queue: QueueServiceClient::new(account, credentials).queue_client(queue_name),
        })
    }
}

impl AzureQueueClient for SdkQueueClient {
    fn receive_one<'s>(
        &'s self,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<'s, Result<Option<AzureRawMessage>, DataError>> {
        Box::pin(async move {
            let builder = self
                .queue
                .get_messages()
                .number_of_messages(NumberOfMessages::new(1u8))
                .visibility_timeout(VisibilityTimeout::new(Duration::from_secs(
                    visibility_timeout_seconds,
                )));
            let response = builder.into_future().await.map_err(azure_error)?;
            Ok(response
                .messages
                .into_iter()
                .next()
                .map(|message| AzureRawMessage {
                    id: message.message_id,
                    pop_receipt: message.pop_receipt,
                    content: message.message_text,
                    dequeue_count: message.dequeue_count,
                }))
        })
    }

    fn update<'s>(
        &'s self,
        message_id: &'s str,
        pop_receipt: &'s str,
        content: &'s str,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<'s, Result<String, DataError>> {
        Box::pin(async move {
            let client = self
                .queue
                .pop_receipt_client(PopReceipt::new(message_id, pop_receipt));
            let response = client
                .update(
                    content,
                    VisibilityTimeout::new(Duration::from_secs(visibility_timeout_seconds)),
                )
                .into_future()
                .await
                .map_err(azure_error)?;
            Ok(response.pop_receipt)
        })
    }

    fn delete<'s>(
        &'s self,
        message_id: &'s str,
        pop_receipt: &'s str,
    ) -> BoxFuture<'s, Result<(), DataError>> {
        Box::pin(async move {
            let client = self
                .queue
                .pop_receipt_client(PopReceipt::new(message_id, pop_receipt));
            client
                .delete()
                .into_future()
                .await
                .map(|_| ())
                .map_err(azure_error)
        })
    }

    fn send<'s>(&'s self, content: String) -> BoxFuture<'s, Result<(), DataError>> {
        Box::pin(async move {
            self.queue
                .put_message(content)
                .into_future()
                .await
                .map(|_| ())
                .map_err(azure_error)
        })
    }
}

fn azure_error(error: azure_core::Error) -> DataError {
    DataError::Io(error.to_string())
}
