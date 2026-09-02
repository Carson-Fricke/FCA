"""At-least-once queue processing for durable market-lake ingest work.

The worker owns queue delivery semantics, not the ingest side effect.  Its
handler must make deterministic, idempotent writes (as the T034 transaction
does), because a completed handler can be redelivered when a process crashes
before deleting its message.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from lake.configuration import QueueSettings


@dataclass(frozen=True, slots=True)
class QueueMessage:
    """The lease-bearing fields needed by a queue worker."""

    message_id: str
    pop_receipt: str
    content: str
    dequeue_count: int

    def __post_init__(self) -> None:
        if not self.message_id.strip():
            raise ValueError("message_id must not be empty")
        if not self.pop_receipt.strip():
            raise ValueError("pop_receipt must not be empty")
        if self.dequeue_count < 1:
            raise ValueError("dequeue_count must be at least 1")


@dataclass(frozen=True, slots=True)
class QueueDelivery:
    """Decoded handler input together with the original delivery metadata."""

    message: QueueMessage
    payload: Mapping[str, object]

    @property
    def message_id(self) -> str:
        """Return the provider message identifier."""
        return self.message.message_id

    @property
    def attempt(self) -> int:
        """Return the provider delivery count for this attempt."""
        return self.message.dequeue_count


class QueueHandler(Protocol):
    """An idempotent async operation for one decoded queue message."""

    async def __call__(self, delivery: QueueDelivery) -> None:
        """Apply the delivery or raise to request an at-least-once retry."""


@runtime_checkable
class QueueTransport(Protocol):
    """Lease-aware queue boundary used by :class:`QueueWorker`."""

    async def receive_one(
        self, *, visibility_timeout_seconds: int
    ) -> QueueMessage | None:
        """Receive one message and make it invisible for the given duration."""

    async def renew(
        self,
        message: QueueMessage,
        *,
        visibility_timeout_seconds: int,
    ) -> QueueMessage:
        """Renew a message lease and return its updated pop receipt."""

    async def delete(self, message: QueueMessage) -> None:
        """Delete a successfully handled or dead-lettered source message."""


@runtime_checkable
class DeadLetterSink(Protocol):
    """Boundary for durable poison-message transfer."""

    async def send(self, message: QueueMessage, *, error: str) -> None:
        """Persist a failed delivery without deleting the source first."""


class WorkerClock(Protocol):
    """Sleep boundary used for lease renewal and empty-queue polling."""

    async def sleep(self, seconds: float) -> None:
        """Suspend for a worker timer interval."""


class AsyncioWorkerClock:
    """Production clock backed by the asyncio event loop."""

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class QueueWorkerPolicy:
    """Validated queue delivery controls derived from T026 settings."""

    visibility_timeout_seconds: int
    visibility_renewal_seconds: int
    max_delivery_attempts: int
    dead_letter_on_exhaustion: bool

    def __post_init__(self) -> None:
        if self.visibility_timeout_seconds < 1:
            raise ValueError("visibility timeout must be positive")
        if self.visibility_renewal_seconds < 1:
            raise ValueError("visibility renewal interval must be positive")
        if self.visibility_renewal_seconds >= self.visibility_timeout_seconds:
            raise ValueError("visibility renewal interval must be shorter than timeout")
        if self.max_delivery_attempts < 1:
            raise ValueError("max delivery attempts must be positive")

    @classmethod
    def from_settings(cls, settings: QueueSettings) -> QueueWorkerPolicy:
        """Build worker controls from the typed T026 queue settings."""
        return cls(
            visibility_timeout_seconds=settings.visibility_timeout_seconds,
            visibility_renewal_seconds=settings.visibility_renewal_seconds,
            max_delivery_attempts=settings.max_delivery_attempts,
            dead_letter_on_exhaustion=settings.dead_letter_on_exhaustion,
        )


class DeliveryOutcome(str):
    """Stable result values returned by one worker delivery."""

    SUCCEEDED = "succeeded"
    RETRY = "retry"
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Observed disposition of one received message."""

    message_id: str
    attempt: int
    outcome: str
    error: str | None = None


class QueueWorker:
    """Process one leased message at a time with safe at-least-once semantics."""

    def __init__(
        self,
        *,
        queue: QueueTransport,
        handler: QueueHandler | Callable[[QueueDelivery], Awaitable[None]],
        policy: QueueWorkerPolicy,
        dead_letter: DeadLetterSink | None = None,
        clock: WorkerClock | None = None,
        idle_sleep_seconds: float = 1.0,
    ) -> None:
        if policy.dead_letter_on_exhaustion and dead_letter is None:
            raise ValueError("dead_letter sink is required when exhaustion is enabled")
        if idle_sleep_seconds <= 0:
            raise ValueError("idle sleep interval must be positive")
        self._queue = queue
        self._handler = handler
        self._policy = policy
        self._dead_letter = dead_letter
        self._clock = AsyncioWorkerClock() if clock is None else clock
        self._idle_sleep_seconds = idle_sleep_seconds

    @classmethod
    def from_settings(
        cls,
        *,
        queue: QueueTransport,
        handler: QueueHandler | Callable[[QueueDelivery], Awaitable[None]],
        settings: QueueSettings,
        dead_letter: DeadLetterSink | None = None,
        clock: WorkerClock | None = None,
        idle_sleep_seconds: float = 1.0,
    ) -> QueueWorker:
        """Construct a worker directly from the typed queue settings."""
        return cls(
            queue=queue,
            handler=handler,
            policy=QueueWorkerPolicy.from_settings(settings),
            dead_letter=dead_letter,
            clock=clock,
            idle_sleep_seconds=idle_sleep_seconds,
        )

    async def process_one(self) -> DeliveryResult | None:
        """Receive and process one message, or return ``None`` when the queue is empty."""
        message = await self._queue.receive_one(
            visibility_timeout_seconds=self._policy.visibility_timeout_seconds
        )
        if message is None:
            return None
        if message.dequeue_count > self._policy.max_delivery_attempts:
            return await self._exhausted(message, "delivery attempt limit exceeded")

        current = message
        finished = asyncio.Event()
        renewal_error: Exception | None = None

        async def renew_lease() -> None:
            nonlocal current, renewal_error
            try:
                while not finished.is_set():
                    await self._clock.sleep(self._policy.visibility_renewal_seconds)
                    if finished.is_set():
                        return
                    current = await self._queue.renew(
                        current,
                        visibility_timeout_seconds=self._policy.visibility_timeout_seconds,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                renewal_error = error

        renewal_task = asyncio.create_task(renew_lease())
        handler_error: Exception | None = None
        try:
            delivery = QueueDelivery(current, _decode_payload(current.content))
            await self._handler(delivery)
        except Exception as error:
            handler_error = error
        finally:
            finished.set()
            renewal_task.cancel()
            await asyncio.gather(renewal_task, return_exceptions=True)

        error = handler_error or renewal_error
        if error is not None:
            return await self._failed(current, _error_message(error))

        await self._queue.delete(current)
        return DeliveryResult(
            message_id=current.message_id,
            attempt=current.dequeue_count,
            outcome=DeliveryOutcome.SUCCEEDED,
        )

    async def run(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        max_messages: int | None = None,
    ) -> tuple[DeliveryResult, ...]:
        """Run until stopped; ``max_messages`` provides a deterministic test boundary."""
        if max_messages is not None and max_messages < 0:
            raise ValueError("max_messages must not be negative")
        results: list[DeliveryResult] = []
        while max_messages is None or len(results) < max_messages:
            if stop_event is not None and stop_event.is_set():
                break
            result = await self.process_one()
            if result is None:
                await self._clock.sleep(self._idle_sleep_seconds)
                continue
            results.append(result)
        return tuple(results)

    async def _failed(self, message: QueueMessage, error: str) -> DeliveryResult:
        if message.dequeue_count < self._policy.max_delivery_attempts:
            return DeliveryResult(
                message_id=message.message_id,
                attempt=message.dequeue_count,
                outcome=DeliveryOutcome.RETRY,
                error=error,
            )
        return await self._exhausted(message, error)

    async def _exhausted(self, message: QueueMessage, error: str) -> DeliveryResult:
        if not self._policy.dead_letter_on_exhaustion:
            return DeliveryResult(
                message_id=message.message_id,
                attempt=message.dequeue_count,
                outcome=DeliveryOutcome.RETRY,
                error=error,
            )
        if self._dead_letter is None:
            raise RuntimeError("dead-letter sink is required for an exhausted message")
        await self._dead_letter.send(message, error=error)
        await self._queue.delete(message)
        return DeliveryResult(
            message_id=message.message_id,
            attempt=message.dequeue_count,
            outcome=DeliveryOutcome.DEAD_LETTERED,
            error=error,
        )


def _decode_payload(content: str) -> Mapping[str, object]:
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("queue message content must be valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise ValueError("queue message content must be a JSON object")
    return cast(Mapping[str, object], decoded)


def _error_message(error: BaseException) -> str:
    detail = str(error).strip()
    return type(error).__name__ if not detail else f"{type(error).__name__}: {detail}"


__all__ = [
    "AsyncioWorkerClock",
    "DeadLetterSink",
    "DeliveryOutcome",
    "DeliveryResult",
    "QueueDelivery",
    "QueueHandler",
    "QueueMessage",
    "QueueTransport",
    "QueueWorker",
    "QueueWorkerPolicy",
    "WorkerClock",
]
