"""Canonical delivery and success-bound consumption for one live session."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from threading import RLock

from thesis.agent.messages import SessionMessage


class SessionInbox:
    """Keep durable arrivals distinct from outgoing records and consumption.

    The writer returns the assigned durable message. Repeated deliveries of an
    already known message ID neither write nor consume it a second time.
    """

    def __init__(
        self,
        history: Sequence[SessionMessage] = (),
        *,
        pending_ids: Collection[str] = (),
        persist: Callable[[SessionMessage], SessionMessage] | None = None,
    ) -> None:
        self._persist = persist
        self._lock = RLock()
        self._messages: dict[str, SessionMessage] = {}
        self._pending_ids = set(pending_ids)
        self._arrivals: list[SessionMessage] = []
        for message in history:
            known = self._messages.get(message.message_id)
            if known is not None:
                if known != message:
                    raise ValueError("conflicting session message identity in history")
                continue
            self._messages[message.message_id] = message
            self._arrivals.append(message)
        if any(
            identity not in self._messages
            or self._messages[identity].direction != "incoming"
            for identity in self._pending_ids
        ):
            raise ValueError("pending IDs must identify incoming history messages")

    def deliver(self, message: SessionMessage) -> SessionMessage:
        """Persist and enqueue an incoming message exactly once by identity."""
        if message.direction != "incoming":
            raise ValueError("session delivery requires an incoming message")
        with self._lock:
            known = self._messages.get(message.message_id)
            if known is not None:
                if known.content != message.content or (
                    message.position != 0 and known.position != message.position
                ):
                    raise ValueError("conflicting session message identity in delivery")
                return known
            durable = self._persist(message) if self._persist is not None else message
            if durable.direction != "incoming":
                raise ValueError("delivery writer must return an incoming message")
            if durable.message_id != message.message_id:
                raise ValueError("delivery writer must preserve message identity")
            if durable.content != message.content:
                raise ValueError("delivery writer must preserve message content")
            self._messages[durable.message_id] = durable
            self._pending_ids.add(durable.message_id)
            index = len(self._arrivals)
            # Positions order incoming peers, never move evidence across output.
            if durable.position:
                while index > 0:
                    previous = self._arrivals[index - 1]
                    if (
                        previous.direction != "incoming"
                        or previous.position <= durable.position
                    ):
                        break
                    index -= 1
            self._arrivals.insert(index, durable)
            return durable

    def record_output(self, message: SessionMessage) -> None:
        """Record model output without treating it as a delivered input."""
        if message.direction != "outgoing":
            raise ValueError("session output requires an outgoing message")
        with self._lock:
            if message.message_id in self._messages:
                raise ValueError("session output message identity already exists")
            self._messages[message.message_id] = message
            self._arrivals.append(message)

    def reconcile(self, messages: Sequence[SessionMessage]) -> None:
        """Apply authoritative scoped order without changing consumption state.

        Omitted bootstrap or not-yet-durable rows retain their slots. The reader
        owns scope and history bounds; unknown outputs cannot be reconstructed.
        New incoming rows use the same idempotent delivery writer as producers.
        """
        with self._lock:
            identities = {message.message_id for message in messages}
            if len(identities) != len(messages):
                raise ValueError("reconciliation message identities must be unique")
            for message in messages:
                known = self._messages.get(message.message_id)
                if known is not None and known != message:
                    raise ValueError(
                        "conflicting session message identity in reconciliation"
                    )
                if known is None and message.direction != "incoming":
                    raise ValueError(
                        "reconciliation cannot introduce unknown outputs or instructions"
                    )
            for message in messages:
                if message.message_id not in self._messages:
                    durable = self.deliver(message)
                    if durable != message:
                        raise ValueError(
                            "reconciliation requires assigned durable messages"
                        )
            ordered = iter(self._messages[message.message_id] for message in messages)
            self._arrivals = [
                next(ordered) if message.message_id in identities else message
                for message in self._arrivals
            ]

    def pending(self) -> tuple[SessionMessage, ...]:
        """Return unconsumed inputs without changing canonical arrival order."""
        with self._lock:
            return tuple(
                message
                for message in self._arrivals
                if message.message_id in self._pending_ids
            )

    def transcript(self) -> tuple[SessionMessage, ...]:
        """Snapshot the complete canonical arrival sequence exactly once."""
        return self.snapshot()[0]

    def snapshot(self) -> tuple[tuple[SessionMessage, ...], tuple[SessionMessage, ...]]:
        """Atomically capture provider history and its acknowledgement batch."""
        with self._lock:
            pending = self.pending()
            return tuple(self._arrivals), pending

    def acknowledge(self, messages: Sequence[SessionMessage]) -> None:
        """Consume only the arrivals included in a successful model request."""
        with self._lock:
            for message in messages:
                if self._messages.get(message.message_id) != message:
                    raise ValueError("cannot acknowledge an unknown session message")
            self._pending_ids.difference_update(
                message.message_id for message in messages
            )
