//! Dead-letter envelopes and explicit recovery for queue deliveries.
//!
//! Ports `lake.queue.dead_letter`. The queue worker owns delivery attempts
//! and source-message deletion; this module owns the durable diagnostic shape
//! written by the dead-letter sink and the operator-triggered replay path.
//! Both boundaries are injected so recovery behavior is testable without a
//! live queue.
//!
//! Python's `DeadLetterFormatError(ValueError)` and plain `ValueError`
//! failures both surface as [`DataError`] values in Rust, carrying the exact
//! Python message text.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use futures_util::future::BoxFuture;
use lake_contracts::{DataError, DateTime};
use lake_storage::QueueSender;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::worker::{DeadLetterSink, QueueMessage};

/// The only supported dead-letter envelope schema version.
pub const DEAD_LETTER_SCHEMA_VERSION: u64 = 1;

/// Envelope fields every dead-letter message must carry.
const REQUIRED_FIELDS: [&str; 9] = [
    "dead_letter_id",
    "dequeue_count",
    "error",
    "failed_at",
    "original_content",
    "schema_version",
    "source_message_id",
    "source_pop_receipt",
    "source_queue",
];

/// Envelope fields that must arrive as JSON strings.
const STRING_FIELDS: [&str; 6] = [
    "dead_letter_id",
    "error",
    "original_content",
    "source_message_id",
    "source_pop_receipt",
    "source_queue",
];

/// A versioned, replayable record of one exhausted queue delivery.
///
/// `original_content` is retained as received rather than reconstructed from
/// decoded JSON, preserving the evidence needed to diagnose a bad producer and
/// letting an operator choose a repaired payload when necessary.
/// `dead_letter_id` deliberately excludes the pop receipt and attempt count,
/// which makes a repeated transfer of the same logical message recognizable
/// after a crash between dead-letter send and source deletion.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeadLetterEnvelope {
    /// The stable identifier of the logical source message.
    pub dead_letter_id: String,
    /// The queue the message was consumed from.
    pub source_queue: String,
    /// The provider message identifier.
    pub source_message_id: String,
    /// The pop receipt at the moment of failure.
    pub source_pop_receipt: String,
    /// The original message body, retained as received.
    pub original_content: String,
    /// The provider delivery count at the moment of failure.
    pub dequeue_count: u32,
    /// The Python-style error text.
    pub error: String,
    /// The failure time, normalized to UTC.
    pub failed_at: DateTime,
    /// The envelope schema version.
    pub schema_version: u64,
}

impl DeadLetterEnvelope {
    #[allow(clippy::too_many_arguments)]
    fn new(
        dead_letter_id: String,
        source_queue: String,
        source_message_id: String,
        source_pop_receipt: String,
        original_content: String,
        dequeue_count: i64,
        error: String,
        failed_at: DateTime,
        schema_version: i64,
    ) -> Result<Self, DataError> {
        if schema_version != DEAD_LETTER_SCHEMA_VERSION as i64 {
            return Err(dead_letter_format_error(format!(
                "unsupported dead-letter schema version: {schema_version}"
            )));
        }
        for (name, value) in [
            ("dead_letter_id", &dead_letter_id),
            ("source_queue", &source_queue),
            ("source_message_id", &source_message_id),
            ("source_pop_receipt", &source_pop_receipt),
            ("error", &error),
        ] {
            if value.trim().is_empty() {
                return Err(DataError::value(format!("{name} must not be empty")));
            }
        }
        if dequeue_count < 1 {
            return Err(DataError::value("dequeue_count must be at least 1"));
        }
        let dequeue_count = u32::try_from(dequeue_count).expect("validated dequeue count fits u32");
        // `DateTime` is a UTC instant, so the Python "failed_at must be
        // timezone-aware" rule is enforced where instants are parsed.
        Ok(Self {
            dead_letter_id,
            source_queue,
            source_message_id,
            source_pop_receipt,
            original_content,
            dequeue_count,
            error,
            failed_at,
            schema_version: DEAD_LETTER_SCHEMA_VERSION,
        })
    }

    /// Create an envelope with a stable ID for the logical source message.
    pub fn from_failure(
        message: &QueueMessage,
        source_queue: &str,
        error: &str,
        failed_at: DateTime,
    ) -> Result<Self, DataError> {
        if source_queue.trim().is_empty() {
            return Err(DataError::value("source_queue must not be empty"));
        }
        if error.trim().is_empty() {
            return Err(DataError::value("error must not be empty"));
        }
        let dead_letter_id =
            stable_dead_letter_id(source_queue, &message.message_id, &message.content);
        Self::new(
            dead_letter_id,
            source_queue.to_string(),
            message.message_id.clone(),
            message.pop_receipt.clone(),
            message.content.clone(),
            i64::from(message.dequeue_count),
            error.to_string(),
            failed_at,
            DEAD_LETTER_SCHEMA_VERSION as i64,
        )
    }

    /// The exception type when the error text carries a Python type name.
    pub fn error_type(&self) -> &str {
        let error_type = self
            .error
            .split_once(": ")
            .map_or(&*self.error, |(head, _)| head);
        if is_python_identifier(error_type) {
            error_type
        } else {
            "UnknownError"
        }
    }

    /// The detail portion while retaining the full `error` field.
    pub fn error_message(&self) -> &str {
        match self.error.split_once(": ") {
            Some((error_type, detail)) if is_python_identifier(error_type) => detail,
            _ => &self.error,
        }
    }

    /// The deterministic JSON object sent to the dead-letter queue.
    pub fn as_message(&self) -> Value {
        let mut object = Map::new();
        object.insert("dequeue_count".into(), Value::from(self.dequeue_count));
        object.insert(
            "dead_letter_id".into(),
            Value::from(self.dead_letter_id.as_str()),
        );
        object.insert("error".into(), Value::from(self.error.as_str()));
        object.insert("error_message".into(), Value::from(self.error_message()));
        object.insert("error_type".into(), Value::from(self.error_type()));
        object.insert(
            "failed_at".into(),
            Value::from(isoformat_utc(self.failed_at)),
        );
        object.insert(
            "original_content".into(),
            Value::from(self.original_content.as_str()),
        );
        object.insert("schema_version".into(), Value::from(self.schema_version));
        object.insert(
            "source_message_id".into(),
            Value::from(self.source_message_id.as_str()),
        );
        object.insert(
            "source_pop_receipt".into(),
            Value::from(self.source_pop_receipt.as_str()),
        );
        object.insert(
            "source_queue".into(),
            Value::from(self.source_queue.as_str()),
        );
        Value::Object(object)
    }

    /// Decode and validate a message read from the dead-letter queue.
    pub fn from_message(message: &Value) -> Result<Self, DataError> {
        let missing: Vec<&str> = REQUIRED_FIELDS
            .iter()
            .copied()
            .filter(|field| message.get(*field).is_none())
            .collect();
        if !missing.is_empty() {
            return Err(dead_letter_format_error(format!(
                "dead-letter message is missing fields: {}",
                missing.join(", ")
            )));
        }

        let failed_at_text = match message.get("failed_at") {
            Some(Value::String(text)) => text.clone(),
            _ => {
                return Err(dead_letter_format_error(
                    "failed_at must be an ISO-8601 string",
                ))
            }
        };
        let failed_at = DateTime::parse_iso_utc(&failed_at_text)
            .ok_or_else(|| dead_letter_format_error("failed_at must be valid ISO-8601"))?;
        if !has_explicit_offset(&failed_at_text) {
            return Err(DataError::value("failed_at must be timezone-aware"));
        }

        let schema_version = required_integer(message.get("schema_version"), "schema_version")?;
        let dequeue_count = required_integer(message.get("dequeue_count"), "dequeue_count")?;

        let mut strings = BTreeMap::new();
        for field in STRING_FIELDS {
            match message.get(field) {
                Some(Value::String(text)) => {
                    strings.insert(field, text.clone());
                }
                _ => {
                    return Err(dead_letter_format_error(
                        "dead-letter string fields must be strings",
                    ))
                }
            }
        }

        Self::new(
            strings["dead_letter_id"].clone(),
            strings["source_queue"].clone(),
            strings["source_message_id"].clone(),
            strings["source_pop_receipt"].clone(),
            strings["original_content"].clone(),
            dequeue_count,
            strings["error"].clone(),
            failed_at,
            schema_version,
        )
    }

    /// The original queue payload when it is a JSON object.
    pub fn replay_payload(&self) -> Result<Value, DataError> {
        let decoded: Value = serde_json::from_str(&self.original_content).map_err(|_| {
            dead_letter_format_error(
                "original_content is not valid JSON; supply a repaired payload",
            )
        })?;
        if !decoded.is_object() {
            return Err(dead_letter_format_error(
                "original_content is not a JSON object; supply a repaired payload",
            ));
        }
        Ok(decoded)
    }
}

/// Diagnostic index for envelopes obtained from a dead-letter queue.
pub trait DeadLetterStore: Send + Sync {
    /// Insert or replace an envelope by its stable dead-letter ID.
    fn put(&self, envelope: DeadLetterEnvelope) -> DeadLetterEnvelope;

    /// Return one envelope by ID, if indexed.
    fn get(&self, dead_letter_id: &str) -> Option<DeadLetterEnvelope>;

    /// Return indexed envelopes in failure-time and ID order.
    fn list(&self) -> Vec<DeadLetterEnvelope>;
}

/// Deterministic diagnostic index used by tests and local recovery tools.
#[derive(Default)]
pub struct InMemoryDeadLetterStore {
    envelopes: Mutex<BTreeMap<String, DeadLetterEnvelope>>,
}

impl InMemoryDeadLetterStore {
    /// Build an empty store.
    pub fn new() -> Self {
        Self::default()
    }

    /// Build a store seeded with envelopes, keyed by dead-letter ID.
    pub fn with_envelopes(envelopes: impl IntoIterator<Item = DeadLetterEnvelope>) -> Self {
        let store = Self::default();
        for envelope in envelopes {
            store
                .envelopes
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .insert(envelope.dead_letter_id.clone(), envelope);
        }
        store
    }
}

impl DeadLetterStore for InMemoryDeadLetterStore {
    fn put(&self, envelope: DeadLetterEnvelope) -> DeadLetterEnvelope {
        let mut envelopes = self
            .envelopes
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        envelopes.insert(envelope.dead_letter_id.clone(), envelope.clone());
        envelope
    }

    fn get(&self, dead_letter_id: &str) -> Option<DeadLetterEnvelope> {
        let envelopes = self
            .envelopes
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        envelopes.get(dead_letter_id).cloned()
    }

    fn list(&self) -> Vec<DeadLetterEnvelope> {
        let envelopes = self
            .envelopes
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let mut items: Vec<DeadLetterEnvelope> = envelopes.values().cloned().collect();
        items.sort_by(|left, right| {
            (left.failed_at, &left.dead_letter_id).cmp(&(right.failed_at, &right.dead_letter_id))
        });
        items
    }
}

/// Write structured dead-letter envelopes through an injected [`QueueSender`].
pub struct QueueDeadLetterSink {
    sender: Arc<dyn QueueSender>,
    source_queue: String,
    clock: Arc<dyn Fn() -> DateTime + Send + Sync>,
    store: Option<Arc<dyn DeadLetterStore>>,
}

impl QueueDeadLetterSink {
    /// Build a sink over a sender and its source-queue name.
    pub fn new(
        sender: Arc<dyn QueueSender>,
        source_queue: impl Into<String>,
    ) -> Result<Self, DataError> {
        Self::with_parts(sender, source_queue, None, None)
    }

    /// Build a sink with an injected failure clock and optional diagnostic store.
    pub fn with_parts(
        sender: Arc<dyn QueueSender>,
        source_queue: impl Into<String>,
        clock: Option<Arc<dyn Fn() -> DateTime + Send + Sync>>,
        store: Option<Arc<dyn DeadLetterStore>>,
    ) -> Result<Self, DataError> {
        let source_queue = source_queue.into();
        if source_queue.trim().is_empty() {
            return Err(DataError::value("source_queue must not be empty"));
        }
        Ok(Self {
            sender,
            source_queue,
            clock: clock.unwrap_or_else(|| Arc::new(system_utc)),
            store,
        })
    }
}

impl DeadLetterSink for QueueDeadLetterSink {
    fn send<'a>(
        &'a self,
        message: &'a QueueMessage,
        error: &'a str,
    ) -> BoxFuture<'a, Result<(), DataError>> {
        Box::pin(async move {
            let envelope = DeadLetterEnvelope::from_failure(
                message,
                &self.source_queue,
                error,
                (self.clock)(),
            )?;
            let payload = envelope.as_message();
            self.sender.send(&payload).await?;
            if let Some(store) = &self.store {
                store.put(envelope);
            }
            Ok(())
        })
    }
}

/// Audit details for one explicit replay request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReplayResult {
    /// The replayed envelope's stable ID.
    pub dead_letter_id: String,
    /// The queue the envelope was replayed to.
    pub source_queue: String,
    /// The original provider message identifier.
    pub source_message_id: String,
    /// Whether an operator-repaired payload replaced the original.
    pub repaired_payload: bool,
}

/// Replay one envelope to its original queue through an injected sender.
pub struct DeadLetterReplayer {
    sender: Arc<dyn QueueSender>,
    target_queue: String,
}

impl DeadLetterReplayer {
    /// Build a replayer targeting one queue name.
    pub fn new(
        sender: Arc<dyn QueueSender>,
        target_queue: impl Into<String>,
    ) -> Result<Self, DataError> {
        let target_queue = target_queue.into();
        if target_queue.trim().is_empty() {
            return Err(DataError::value("target_queue must not be empty"));
        }
        Ok(Self {
            sender,
            target_queue,
        })
    }

    /// Send one original or operator-repaired JSON object.
    ///
    /// A replay is deliberately explicit and sends the payload without the
    /// dead-letter metadata wrapper, so the normal queue worker handler sees
    /// the same contract as a fresh producer message.
    pub fn replay<'a>(
        &'a self,
        envelope: &'a DeadLetterEnvelope,
        repaired_payload: Option<&'a Value>,
    ) -> BoxFuture<'a, Result<ReplayResult, DataError>> {
        Box::pin(async move {
            if envelope.source_queue != self.target_queue {
                return Err(DataError::value(
                    "dead-letter source queue does not match the replay target queue",
                ));
            }
            let replay_payload;
            let payload = match repaired_payload {
                Some(payload) => payload,
                None => {
                    replay_payload = envelope.replay_payload()?;
                    &replay_payload
                }
            };
            self.sender.send(payload).await?;
            Ok(ReplayResult {
                dead_letter_id: envelope.dead_letter_id.clone(),
                source_queue: envelope.source_queue.clone(),
                source_message_id: envelope.source_message_id.clone(),
                repaired_payload: repaired_payload.is_some(),
            })
        })
    }

    /// Look up and replay one indexed envelope.
    pub async fn replay_from_store(
        &self,
        store: &dyn DeadLetterStore,
        dead_letter_id: &str,
        repaired_payload: Option<&Value>,
    ) -> Result<ReplayResult, DataError> {
        let Some(envelope) = store.get(dead_letter_id) else {
            return Err(DataError::not_found(format!(
                "dead-letter message not found: {dead_letter_id}"
            )));
        };
        self.replay(&envelope, repaired_payload).await
    }
}

/// Build a dead-letter format failure with the Python message text.
fn dead_letter_format_error(message: impl Into<String>) -> DataError {
    DataError::value(message.into())
}

/// Require a JSON number field, rejecting booleans like Python's int check.
fn required_integer(value: Option<&Value>, name: &str) -> Result<i64, DataError> {
    value
        .and_then(Value::as_i64)
        .ok_or_else(|| dead_letter_format_error(format!("{name} must be an integer")))
}

/// Recognize the Python identifiers used for `error_type`.
fn is_python_identifier(value: &str) -> bool {
    let mut characters = value.chars();
    let starts_valid = matches!(
        characters.next(),
        Some(first) if first.is_ascii_alphabetic() || first == '_'
    );
    starts_valid && characters.all(|rest| rest.is_ascii_alphanumeric() || rest == '_')
}

/// Render a UTC instant like Python's `datetime.isoformat()` on a UTC-aware
/// value: no fractional part when microseconds are zero, and a `+00:00`
/// offset.
fn isoformat_utc(instant: DateTime) -> String {
    let text = instant.isoformat_micros();
    let body = text.strip_suffix(".000000").unwrap_or(&text);
    format!("{body}+00:00")
}

/// Require an explicit timezone offset, matching Python's tz-aware envelope.
fn has_explicit_offset(text: &str) -> bool {
    text.ends_with('Z') || text.rfind(['+', '-']).is_some_and(|index| index > 10)
}

/// The current wall-clock instant as a UTC `DateTime`.
fn system_utc() -> DateTime {
    let elapsed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("system clock is set after the Unix epoch");
    DateTime::from_micros(elapsed.as_micros() as i64)
}

/// Hash the logical message identity into the stable dead-letter ID.
///
/// Mirrors the Python helper exactly: a compact, key-sorted,
/// ASCII-escaped JSON object over `(content, message_id, source_queue)`.
fn stable_dead_letter_id(source_queue: &str, message_id: &str, content: &str) -> String {
    let identity = format!(
        "{{\"content\":{},\"message_id\":{},\"source_queue\":{}}}",
        json_ensure_ascii_string(content),
        json_ensure_ascii_string(message_id),
        json_ensure_ascii_string(source_queue),
    );
    hex::encode(Sha256::digest(identity.as_bytes()))
}

/// Render one string the way `json.dumps(..., ensure_ascii=True)` does.
fn json_ensure_ascii_string(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len() + 2);
    encoded.push('"');
    for unit in value.encode_utf16() {
        match unit {
            0x08 => encoded.push_str("\\b"),
            0x09 => encoded.push_str("\\t"),
            0x0A => encoded.push_str("\\n"),
            0x0C => encoded.push_str("\\f"),
            0x0D => encoded.push_str("\\r"),
            0x22 => encoded.push_str("\\\""),
            0x5C => encoded.push_str("\\\\"),
            0x20..=0x7E => encoded.push(unit as u8 as char),
            _ => encoded.push_str(&format!("\\u{unit:04x}")),
        }
    }
    encoded.push('"');
    encoded
}
