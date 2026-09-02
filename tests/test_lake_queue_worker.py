import asyncio
from dataclasses import replace
from typing import Any

import pytest

from lake.queue_worker import (
    DeadLetterSink,
    DeliveryOutcome,
    DeliveryResult,
    QueueDelivery,
    QueueMessage,
    QueueTransport,
    QueueWorker,
    QueueWorkerPolicy,
)
from lake.settings import QueueSettings


class FakeClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.renewal_ready = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds == 2:
            await self.renewal_ready.wait()
            self.renewal_ready.clear()
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(0)


class FakeQueue:
    def __init__(self, *messages: QueueMessage) -> None:
        self.messages = list(messages)
        self.received: list[tuple[QueueMessage, int]] = []
        self.renewals: list[tuple[QueueMessage, int]] = []
        self.deleted: list[QueueMessage] = []

    async def receive_one(
        self, *, visibility_timeout_seconds: int
    ) -> QueueMessage | None:
        if not self.messages:
            return None
        message = self.messages.pop(0)
        self.received.append((message, visibility_timeout_seconds))
        return message

    async def renew(
        self,
        message: QueueMessage,
        *,
        visibility_timeout_seconds: int,
    ) -> QueueMessage:
        self.renewals.append((message, visibility_timeout_seconds))
        return replace(message, pop_receipt=f"{message.pop_receipt}-renewed")

    async def delete(self, message: QueueMessage) -> None:
        self.deleted.append(message)


class FakeDeadLetter:
    def __init__(self) -> None:
        self.sent: list[tuple[QueueMessage, str]] = []

    async def send(self, message: QueueMessage, *, error: str) -> None:
        self.sent.append((message, error))


def _message(*, attempt: int = 1, content: str = '{"job_id":"job-1"}') -> QueueMessage:
    return QueueMessage(
        message_id="message-1",
        pop_receipt=f"receipt-{attempt}",
        content=content,
        dequeue_count=attempt,
    )


def _policy(*, attempts: int = 3, dead_letter: bool = True) -> QueueWorkerPolicy:
    return QueueWorkerPolicy(
        visibility_timeout_seconds=10,
        visibility_renewal_seconds=2,
        max_delivery_attempts=attempts,
        dead_letter_on_exhaustion=dead_letter,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_policy_is_derived_from_t026_settings_and_renewal_is_before_timeout() -> None:
    settings = QueueSettings(
        visibility_timeout_seconds=300,
        visibility_renewal_seconds=60,
        max_delivery_attempts=5,
        dead_letter_on_exhaustion=True,
    )

    policy = QueueWorkerPolicy.from_settings(settings)

    assert policy == QueueWorkerPolicy(300, 60, 5, True)
    with pytest.raises(ValueError, match="shorter than timeout"):
        QueueWorkerPolicy(10, 10, 3, True)


def test_success_decodes_payload_and_deletes_only_after_handler_returns() -> None:
    queue = FakeQueue(_message())
    handled: list[QueueDelivery] = []

    async def handler(delivery: QueueDelivery) -> None:
        handled.append(delivery)

    result = _run(
        QueueWorker(
            queue=queue,
            handler=handler,
            policy=_policy(),
            dead_letter=FakeDeadLetter(),
        ).process_one()
    )

    assert result == DeliveryResult("message-1", 1, DeliveryOutcome.SUCCEEDED)
    assert handled[0].payload == {"job_id": "job-1"}
    assert queue.deleted == [_message()]


def test_long_handler_renews_visibility_and_deletes_with_new_pop_receipt() -> None:
    queue = FakeQueue(_message())
    clock = FakeClock()
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def handler(delivery: QueueDelivery) -> None:
        handler_started.set()
        await release_handler.wait()

    async def scenario() -> Any:
        worker = QueueWorker(
            queue=queue,
            handler=handler,
            policy=_policy(),
            dead_letter=FakeDeadLetter(),
            clock=clock,
        )
        task = asyncio.create_task(worker.process_one())
        await handler_started.wait()
        await asyncio.sleep(0)
        assert not queue.renewals
        clock.renewal_ready.set()
        await asyncio.sleep(0)
        release_handler.set()
        return await task

    result = _run(scenario())

    assert result is not None
    assert result.outcome == DeliveryOutcome.SUCCEEDED
    assert queue.received == [(_message(), 10)]
    assert queue.renewals == [(_message(), 10)]
    assert queue.deleted == [replace(_message(), pop_receipt="receipt-1-renewed")]


def test_failed_handler_is_left_for_retry_before_attempt_limit() -> None:
    queue = FakeQueue(_message(attempt=1))
    dead_letter = FakeDeadLetter()

    async def handler(delivery: QueueDelivery) -> None:
        raise RuntimeError("provider unavailable")

    result = _run(
        QueueWorker(
            queue=queue,
            handler=handler,
            policy=_policy(attempts=3),
            dead_letter=dead_letter,
        ).process_one()
    )

    assert result == DeliveryResult(
        "message-1",
        1,
        DeliveryOutcome.RETRY,
        "RuntimeError: provider unavailable",
    )
    assert queue.deleted == []
    assert dead_letter.sent == []


def test_final_failed_attempt_is_dead_lettered_before_source_delete() -> None:
    queue = FakeQueue(_message(attempt=3))
    dead_letter = FakeDeadLetter()

    async def handler(delivery: QueueDelivery) -> None:
        raise ValueError("bad source response")

    result = _run(
        QueueWorker(
            queue=queue,
            handler=handler,
            policy=_policy(attempts=3),
            dead_letter=dead_letter,
        ).process_one()
    )

    assert result is not None
    assert result.outcome == DeliveryOutcome.DEAD_LETTERED
    assert dead_letter.sent == [
        (_message(attempt=3), "ValueError: bad source response")
    ]
    assert queue.deleted == [_message(attempt=3)]


def test_disabled_dead_letter_policy_leaves_exhausted_message_for_boundary_recovery() -> (
    None
):
    queue = FakeQueue(_message(attempt=2))
    called = False

    async def handler(delivery: QueueDelivery) -> None:
        nonlocal called
        called = True
        raise RuntimeError("still unavailable")

    result = _run(
        QueueWorker(
            queue=queue,
            handler=handler,
            policy=_policy(attempts=2, dead_letter=False),
        ).process_one()
    )

    assert result is not None
    assert called
    assert result.outcome == DeliveryOutcome.RETRY
    assert queue.deleted == []


def test_delivery_past_attempt_limit_is_not_handled_again() -> None:
    queue = FakeQueue(_message(attempt=4))
    dead_letter = FakeDeadLetter()
    handled = 0

    async def handler(delivery: QueueDelivery) -> None:
        nonlocal handled
        handled += 1

    result = _run(
        QueueWorker(
            queue=queue,
            handler=handler,
            policy=_policy(attempts=3),
            dead_letter=dead_letter,
        ).process_one()
    )

    assert result is not None
    assert result.outcome == DeliveryOutcome.DEAD_LETTERED
    assert handled == 0
    assert dead_letter.sent == [
        (_message(attempt=4), "delivery attempt limit exceeded")
    ]
    assert queue.deleted == [_message(attempt=4)]


def test_duplicate_delivery_uses_idempotent_handler_and_deterministic_result() -> None:
    first = _message()
    duplicate = replace(first, message_id="message-2", pop_receipt="receipt-2")
    queue = FakeQueue(first, duplicate)
    objects: dict[str, str] = {}
    catalog: set[str] = set()

    async def handler(delivery: QueueDelivery) -> None:
        job_id = str(delivery.payload["job_id"])
        objects[job_id] = "published"
        catalog.add(job_id)

    worker = QueueWorker(
        queue=queue,
        handler=handler,
        policy=_policy(),
        dead_letter=FakeDeadLetter(),
    )
    results = _run(worker.run(max_messages=2))

    assert [result.outcome for result in results] == [
        DeliveryOutcome.SUCCEEDED,
        DeliveryOutcome.SUCCEEDED,
    ]
    assert objects == {"job-1": "published"}
    assert catalog == {"job-1"}
    assert queue.deleted == [first, duplicate]


def test_worker_loop_stops_after_requested_deliveries() -> None:
    queue = FakeQueue(_message(), replace(_message(), message_id="message-2"))
    handled: list[str] = []

    async def handler(delivery: QueueDelivery) -> None:
        handled.append(delivery.message_id)

    results = _run(
        QueueWorker(
            queue=queue,
            handler=handler,
            policy=_policy(),
            dead_letter=FakeDeadLetter(),
        ).run(max_messages=2)
    )

    assert len(results) == 2
    assert handled == ["message-1", "message-2"]


def test_exhaustion_requires_a_dead_letter_boundary() -> None:
    with pytest.raises(ValueError, match="dead_letter sink is required"):
        QueueWorker(
            queue=FakeQueue(),
            handler=lambda delivery: _never(delivery),
            policy=_policy(),
        )


async def _never(delivery: QueueDelivery) -> None:
    return None


assert isinstance(FakeQueue(), QueueTransport)
assert isinstance(FakeDeadLetter(), DeadLetterSink)
