import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from lake.queue import (
    DEAD_LETTER_SCHEMA_VERSION,
    DeadLetterEnvelope,
    DeadLetterFormatError,
    DeadLetterReplayer,
    DeadLetterStore,
    DeliveryOutcome,
    InMemoryDeadLetterStore,
    QueueDeadLetterSink,
    QueueMessage,
    QueueTransport,
    QueueWorker,
    QueueWorkerPolicy,
    ReplayResult,
)


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[Mapping[str, object]] = []

    def send(self, message: Mapping[str, object]) -> None:
        self.messages.append(dict(message))


class FakeQueue:
    def __init__(self, *messages: QueueMessage) -> None:
        self.messages = list(messages)
        self.deleted: list[QueueMessage] = []

    async def receive_one(
        self, *, visibility_timeout_seconds: int
    ) -> QueueMessage | None:
        del visibility_timeout_seconds
        return self.messages.pop(0) if self.messages else None

    async def renew(
        self,
        message: QueueMessage,
        *,
        visibility_timeout_seconds: int,
    ) -> QueueMessage:
        del visibility_timeout_seconds
        return message

    async def delete(self, message: QueueMessage) -> None:
        self.deleted.append(message)


def _message(
    *,
    attempt: int = 1,
    content: str = '{"job_id":"job-1"}',
    message_id: str = "message-1",
) -> QueueMessage:
    return QueueMessage(
        message_id=message_id,
        pop_receipt=f"receipt-{attempt}",
        content=content,
        dequeue_count=attempt,
    )


def _policy() -> QueueWorkerPolicy:
    return QueueWorkerPolicy(
        visibility_timeout_seconds=30,
        visibility_renewal_seconds=5,
        max_delivery_attempts=2,
        dead_letter_on_exhaustion=True,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_envelope_preserves_failure_metadata_and_round_trips() -> None:
    failed_at = datetime(2026, 9, 1, 23, 0, tzinfo=UTC)
    envelope = DeadLetterEnvelope.from_failure(
        _message(attempt=3),
        source_queue="equity-backfill",
        error="ValueError: provider response omitted close",
        failed_at=failed_at,
    )

    encoded = envelope.as_message()
    decoded = DeadLetterEnvelope.from_message(encoded)

    assert decoded == envelope
    assert encoded["schema_version"] == DEAD_LETTER_SCHEMA_VERSION
    assert encoded["error_type"] == "ValueError"
    assert encoded["error_message"] == "provider response omitted close"
    assert encoded["source_message_id"] == "message-1"
    assert encoded["original_content"] == '{"job_id":"job-1"}'
    assert len(envelope.dead_letter_id) == 64


def test_queue_worker_stops_after_limit_and_sends_before_source_delete() -> None:
    queue = FakeQueue(_message(attempt=1), _message(attempt=2))
    sender = FakeSender()
    store = InMemoryDeadLetterStore()
    sink = QueueDeadLetterSink(
        cast(Any, sender),
        source_queue="equity-backfill",
        clock=lambda: datetime(2026, 9, 1, 23, 0, tzinfo=UTC),
        store=store,
    )
    handled = 0

    async def handler(delivery: Any) -> None:
        nonlocal handled
        handled += 1
        raise RuntimeError("permanent source contract failure")

    worker = QueueWorker(
        queue=queue,
        handler=handler,
        policy=_policy(),
        dead_letter=sink,
    )
    results = _run(worker.run(max_messages=2))

    assert [result.outcome for result in results] == [
        DeliveryOutcome.RETRY,
        DeliveryOutcome.DEAD_LETTERED,
    ]
    assert handled == 2
    assert len(sender.messages) == 1
    assert len(store.list()) == 1
    assert queue.deleted == [_message(attempt=2)]
    assert json.loads(cast(str, sender.messages[0]["original_content"])) == {
        "job_id": "job-1"
    }


def test_replay_sends_original_payload_and_supports_repaired_payload() -> None:
    sender = FakeSender()
    envelope = DeadLetterEnvelope.from_failure(
        _message(content='{"job_id":"job-1","bad":true}'),
        source_queue="equity-backfill",
        error="ValueError: bad source response",
        failed_at=datetime(2026, 9, 1, 23, 0, tzinfo=UTC),
    )
    replayer = DeadLetterReplayer(cast(Any, sender), target_queue="equity-backfill")

    result = replayer.replay(envelope)
    repaired = replayer.replay(
        replace(envelope, original_content="not-json"),
        repaired_payload={"job_id": "job-1", "repair": "drop-bad-field"},
    )

    assert result == ReplayResult(
        envelope.dead_letter_id,
        "equity-backfill",
        "message-1",
        False,
    )
    assert repaired.repaired_payload
    assert sender.messages == [
        {"job_id": "job-1", "bad": True},
        {"job_id": "job-1", "repair": "drop-bad-field"},
    ]


def test_replay_rejects_invalid_original_and_wrong_target() -> None:
    envelope = DeadLetterEnvelope.from_failure(
        _message(content="not-json"),
        source_queue="equity-backfill",
        error="ValueError: invalid payload",
        failed_at=datetime(2026, 9, 1, 23, 0, tzinfo=UTC),
    )

    with pytest.raises(DeadLetterFormatError, match="supply a repaired payload"):
        DeadLetterReplayer(
            cast(Any, FakeSender()), target_queue="equity-backfill"
        ).replay(envelope)
    with pytest.raises(ValueError, match="does not match"):
        DeadLetterReplayer(
            cast(Any, FakeSender()), target_queue="options-backfill"
        ).replay(envelope, repaired_payload={"job_id": "job-1"})


def test_store_deduplicates_stable_id_and_orders_by_failure_time() -> None:
    first = DeadLetterEnvelope.from_failure(
        _message(message_id="first"),
        source_queue="equity-backfill",
        error="RuntimeError: first",
        failed_at=datetime(2026, 9, 1, 23, 2, tzinfo=UTC),
    )
    second = DeadLetterEnvelope.from_failure(
        _message(message_id="second"),
        source_queue="equity-backfill",
        error="RuntimeError: second",
        failed_at=datetime(2026, 9, 1, 23, 1, tzinfo=UTC),
    )
    store = InMemoryDeadLetterStore([first, second])

    replacement = replace(first, error="RuntimeError: updated")
    assert store.put(replacement) == replacement
    assert store.get(first.dead_letter_id) == replacement
    assert [item.dead_letter_id for item in store.list()] == [
        second.dead_letter_id,
        first.dead_letter_id,
    ]


def test_dead_letter_protocols_are_injected_boundaries() -> None:
    assert isinstance(InMemoryDeadLetterStore(), DeadLetterStore)
    assert isinstance(FakeQueue(), QueueTransport)
