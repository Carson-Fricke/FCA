"""Dead-letter envelopes and explicit recovery for queue deliveries.

The queue worker owns delivery attempts and source-message deletion.  This
module owns the durable diagnostic shape written by the dead-letter sink and
the operator-triggered replay path.  Both Azure boundaries are injected so
the recovery behavior can be tested without a live queue.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast, runtime_checkable

from lake.queue_worker import QueueMessage
from lake.storage import QueueSender

DEAD_LETTER_SCHEMA_VERSION = 1


class DeadLetterFormatError(ValueError):
    """Raised when a dead-letter envelope cannot be safely inspected."""


@dataclass(frozen=True, slots=True)
class DeadLetterEnvelope:
    """A versioned, replayable record of one exhausted queue delivery.

    ``original_content`` is retained as received rather than reconstructed
    from decoded JSON.  This preserves the evidence needed to diagnose a bad
    producer and lets an operator choose a repaired payload when necessary.
    ``dead_letter_id`` deliberately excludes the pop receipt and attempt
    count, which makes a repeated transfer of the same logical message
    recognizable after a crash between dead-letter send and source deletion.
    """

    dead_letter_id: str
    source_queue: str
    source_message_id: str
    source_pop_receipt: str
    original_content: str
    dequeue_count: int
    error: str
    failed_at: datetime
    schema_version: int = DEAD_LETTER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DEAD_LETTER_SCHEMA_VERSION:
            raise DeadLetterFormatError(
                f"unsupported dead-letter schema version: {self.schema_version}"
            )
        for name, value in (
            ("dead_letter_id", self.dead_letter_id),
            ("source_queue", self.source_queue),
            ("source_message_id", self.source_message_id),
            ("source_pop_receipt", self.source_pop_receipt),
            ("error", self.error),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.dequeue_count < 1:
            raise ValueError("dequeue_count must be at least 1")
        if self.failed_at.tzinfo is None or self.failed_at.utcoffset() is None:
            raise ValueError("failed_at must be timezone-aware")

        object.__setattr__(self, "failed_at", self.failed_at.astimezone(UTC))

    @classmethod
    def from_failure(
        cls,
        message: QueueMessage,
        *,
        source_queue: str,
        error: str,
        failed_at: datetime,
    ) -> DeadLetterEnvelope:
        """Create an envelope with a stable ID for the logical source message."""
        if not source_queue.strip():
            raise ValueError("source_queue must not be empty")
        if not error.strip():
            raise ValueError("error must not be empty")
        dead_letter_id = _stable_dead_letter_id(
            source_queue=source_queue,
            message_id=message.message_id,
            content=message.content,
        )
        return cls(
            dead_letter_id=dead_letter_id,
            source_queue=source_queue,
            source_message_id=message.message_id,
            source_pop_receipt=message.pop_receipt,
            original_content=message.content,
            dequeue_count=message.dequeue_count,
            error=error,
            failed_at=failed_at,
        )

    @property
    def error_type(self) -> str:
        """Return the exception type when the worker's error text contains it."""
        error_type, _, _ = self.error.partition(": ")
        return error_type if error_type.isidentifier() else "UnknownError"

    @property
    def error_message(self) -> str:
        """Return the detail portion while retaining the full ``error`` field."""
        error_type, separator, detail = self.error.partition(": ")
        if separator and error_type.isidentifier():
            return detail
        return self.error

    def as_message(self) -> dict[str, object]:
        """Return the deterministic JSON object sent to the dead-letter queue."""
        return {
            "dequeue_count": self.dequeue_count,
            "dead_letter_id": self.dead_letter_id,
            "error": self.error,
            "error_message": self.error_message,
            "error_type": self.error_type,
            "failed_at": self.failed_at.isoformat(),
            "original_content": self.original_content,
            "schema_version": self.schema_version,
            "source_message_id": self.source_message_id,
            "source_pop_receipt": self.source_pop_receipt,
            "source_queue": self.source_queue,
        }

    @classmethod
    def from_message(cls, message: Mapping[str, object]) -> DeadLetterEnvelope:
        """Decode and validate a message read from the dead-letter queue."""
        values = _required_fields(message)
        failed_at_value = values["failed_at"]
        if not isinstance(failed_at_value, str):
            raise DeadLetterFormatError("failed_at must be an ISO-8601 string")
        try:
            failed_at = datetime.fromisoformat(failed_at_value)
        except ValueError as error:
            raise DeadLetterFormatError("failed_at must be valid ISO-8601") from error

        schema_version = values["schema_version"]
        dequeue_count = values["dequeue_count"]
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise DeadLetterFormatError("schema_version must be an integer")
        if not isinstance(dequeue_count, int) or isinstance(dequeue_count, bool):
            raise DeadLetterFormatError("dequeue_count must be an integer")

        string_fields = (
            "dead_letter_id",
            "source_queue",
            "source_message_id",
            "source_pop_receipt",
            "original_content",
            "error",
        )
        if any(not isinstance(values[field], str) for field in string_fields):
            raise DeadLetterFormatError("dead-letter string fields must be strings")

        return cls(
            dead_letter_id=cast(str, values["dead_letter_id"]),
            source_queue=cast(str, values["source_queue"]),
            source_message_id=cast(str, values["source_message_id"]),
            source_pop_receipt=cast(str, values["source_pop_receipt"]),
            original_content=cast(str, values["original_content"]),
            dequeue_count=dequeue_count,
            error=cast(str, values["error"]),
            failed_at=failed_at,
            schema_version=schema_version,
        )

    def replay_payload(self) -> Mapping[str, object]:
        """Return the original queue payload when it is a JSON object."""
        try:
            decoded = json.loads(self.original_content)
        except json.JSONDecodeError as error:
            raise DeadLetterFormatError(
                "original_content is not valid JSON; supply a repaired payload"
            ) from error
        if not isinstance(decoded, Mapping):
            raise DeadLetterFormatError(
                "original_content is not a JSON object; supply a repaired payload"
            )
        return cast(Mapping[str, object], decoded)


@runtime_checkable
class DeadLetterStore(Protocol):
    """Diagnostic index for envelopes obtained from a dead-letter queue."""

    def put(self, envelope: DeadLetterEnvelope) -> DeadLetterEnvelope:
        """Insert or replace an envelope by its stable dead-letter ID."""

    def get(self, dead_letter_id: str) -> DeadLetterEnvelope | None:
        """Return one envelope by ID, if indexed."""

    def list(self) -> tuple[DeadLetterEnvelope, ...]:
        """Return indexed envelopes in failure-time and ID order."""


class InMemoryDeadLetterStore:
    """Deterministic diagnostic index used by tests and local recovery tools."""

    def __init__(self, envelopes: Iterable[DeadLetterEnvelope] = ()) -> None:
        self._envelopes = {envelope.dead_letter_id: envelope for envelope in envelopes}

    def put(self, envelope: DeadLetterEnvelope) -> DeadLetterEnvelope:
        self._envelopes[envelope.dead_letter_id] = envelope
        return envelope

    def get(self, dead_letter_id: str) -> DeadLetterEnvelope | None:
        return self._envelopes.get(dead_letter_id)

    def list(self) -> tuple[DeadLetterEnvelope, ...]:
        return tuple(
            sorted(
                self._envelopes.values(),
                key=lambda envelope: (envelope.failed_at, envelope.dead_letter_id),
            )
        )


class QueueDeadLetterSink:
    """Write structured dead-letter envelopes through an injected QueueSender."""

    def __init__(
        self,
        sender: QueueSender,
        *,
        source_queue: str,
        clock: Callable[[], datetime] | None = None,
        store: DeadLetterStore | None = None,
    ) -> None:
        if not source_queue.strip():
            raise ValueError("source_queue must not be empty")
        self._sender = sender
        self._source_queue = source_queue
        self._clock = clock or (lambda: datetime.now(UTC))
        self._store = store

    async def send(self, message: QueueMessage, *, error: str) -> None:
        """Persist the envelope before QueueWorker deletes the source message."""
        envelope = DeadLetterEnvelope.from_failure(
            message,
            source_queue=self._source_queue,
            error=error,
            failed_at=self._clock(),
        )
        await asyncio.to_thread(self._sender.send, envelope.as_message())
        if self._store is not None:
            self._store.put(envelope)


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Audit details for one explicit replay request."""

    dead_letter_id: str
    source_queue: str
    source_message_id: str
    repaired_payload: bool


class DeadLetterReplayer:
    """Replay one envelope to its original queue through an injected sender."""

    def __init__(self, sender: QueueSender, *, target_queue: str) -> None:
        if not target_queue.strip():
            raise ValueError("target_queue must not be empty")
        self._sender = sender
        self._target_queue = target_queue

    def replay(
        self,
        envelope: DeadLetterEnvelope,
        *,
        repaired_payload: Mapping[str, object] | None = None,
    ) -> ReplayResult:
        """Send one original or operator-repaired JSON object.

        A replay is deliberately explicit and sends the payload without the
        dead-letter metadata wrapper, so the normal QueueWorker handler sees
        the same contract as a fresh producer message.
        """
        if envelope.source_queue != self._target_queue:
            raise ValueError(
                "dead-letter source queue does not match the replay target queue"
            )
        payload = (
            envelope.replay_payload() if repaired_payload is None else repaired_payload
        )
        self._sender.send(payload)
        return ReplayResult(
            dead_letter_id=envelope.dead_letter_id,
            source_queue=envelope.source_queue,
            source_message_id=envelope.source_message_id,
            repaired_payload=repaired_payload is not None,
        )

    def replay_from_store(
        self,
        store: DeadLetterStore,
        dead_letter_id: str,
        *,
        repaired_payload: Mapping[str, object] | None = None,
    ) -> ReplayResult:
        """Look up and replay one indexed envelope."""
        envelope = store.get(dead_letter_id)
        if envelope is None:
            raise KeyError(f"dead-letter message not found: {dead_letter_id}")
        return self.replay(envelope, repaired_payload=repaired_payload)


def _required_fields(message: Mapping[str, object]) -> dict[str, object]:
    required = {
        "dead_letter_id",
        "source_queue",
        "source_message_id",
        "source_pop_receipt",
        "original_content",
        "dequeue_count",
        "error",
        "failed_at",
        "schema_version",
    }
    missing = sorted(required.difference(message))
    if missing:
        raise DeadLetterFormatError(
            f"dead-letter message is missing fields: {', '.join(missing)}"
        )
    return {name: message[name] for name in required}


def _stable_dead_letter_id(*, source_queue: str, message_id: str, content: str) -> str:
    identity = json.dumps(
        {
            "content": content,
            "message_id": message_id,
            "source_queue": source_queue,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


__all__ = [
    "DEAD_LETTER_SCHEMA_VERSION",
    "DeadLetterEnvelope",
    "DeadLetterFormatError",
    "DeadLetterReplayer",
    "DeadLetterStore",
    "InMemoryDeadLetterStore",
    "QueueDeadLetterSink",
    "ReplayResult",
]
