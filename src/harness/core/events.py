"""Single-threaded deterministic event queue and synchronous event bus."""

from __future__ import annotations

import hashlib
import heapq
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import TypeVar, cast, overload

from harness.core.clock import AdvanceableClock, Clock
from harness.core.types.events import Event, EventType

EventT = TypeVar("EventT", bound=Event)
EventHandler = Callable[[EventT], None]
EventSelector = type[Event] | EventType

# EventType is serialized, so this explicit table is also the public ordering
# contract. Insertion order and Python class names never affect priority.
EVENT_CLASS_PRIORITY: Mapping[EventType, int] = MappingProxyType(
    {
        EventType.QUOTE: 0,
        EventType.TRADE: 1,
        EventType.ORDER: 2,
        EventType.MULTI_LEG_ORDER: 3,
        EventType.EXECUTION_REPORT: 4,
        EventType.FILL: 5,
        EventType.POSITION: 6,
        EventType.BOOK: 7,
        EventType.ACCOUNT: 8,
    }
)


@dataclass(order=True, slots=True)
class _QueuedEvent:
    timestamp: datetime
    sequence: int
    class_priority: int
    feed_ordinal: int
    event: Event = field(compare=False)


class EventQueue:
    """Priority queue ordered by time, source sequence, class, then feed order.

    The queue intentionally has no locking. A caller owns it from one event
    loop and feeds events in the order received from each normalized feed.
    """

    __slots__ = ("_clock", "_feed_ordinal", "_heap", "_last_dispatched_key")

    def __init__(self, clock: Clock) -> None:
        if not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        self._clock = clock
        self._heap: list[_QueuedEvent] = []
        self._feed_ordinal = 0
        self._last_dispatched_key: tuple[datetime, int, int] | None = None

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)

    def enqueue(self, event: Event) -> None:
        """Queue an event while preserving insertion order for exact ties."""
        if not isinstance(event, Event):
            raise TypeError("event must be an Event")
        priority = EVENT_CLASS_PRIORITY[event.event_type]
        ordering_key = (event.timestamp, event.sequence, priority)
        minimum = (
            None if self._last_dispatched_key is None else self._last_dispatched_key[0]
        )
        if isinstance(self._clock, AdvanceableClock):
            minimum = self._clock.now()
        if minimum is not None and event.timestamp < minimum:
            raise ValueError("event timestamp cannot be older than the clock head")
        if (
            self._last_dispatched_key is not None
            and ordering_key < self._last_dispatched_key
        ):
            raise ValueError("event ordering key cannot precede the last dispatch")
        heapq.heappush(
            self._heap,
            _QueuedEvent(
                event.timestamp,
                event.sequence,
                priority,
                self._feed_ordinal,
                event,
            ),
        )
        self._feed_ordinal += 1

    def enqueue_many(self, events: Iterable[Event]) -> None:
        """Queue events in iteration order, which defines their feed order."""
        for event in events:
            self.enqueue(event)

    def peek(self) -> Event | None:
        """Return the next event without removing it."""
        return None if not self._heap else self._heap[0].event

    def pop(self) -> Event | None:
        """Return the next dispatchable event, or ``None`` if none is due."""
        if not self._heap:
            return None
        queued = self._heap[0]
        if isinstance(self._clock, AdvanceableClock):
            self._clock.advance_to(queued.timestamp)
        elif queued.timestamp > self._clock.now():
            return None
        heapq.heappop(self._heap)
        self._last_dispatched_key = (
            queued.timestamp,
            queued.sequence,
            queued.class_priority,
        )
        return queued.event


class _Subscription:
    __slots__ = ("handler", "selector")

    def __init__(self, selector: EventSelector, handler: EventHandler[Event]) -> None:
        self.selector = selector
        self.handler = handler

    def matches(self, event: Event) -> bool:
        if isinstance(self.selector, EventType):
            return event.event_type is self.selector
        return isinstance(event, self.selector)

    def dispatch(self, event: Event) -> None:
        if self.matches(event):
            self.handler(event)


class EventBus:
    """Dispatch queued events synchronously in one deterministic event loop."""

    __slots__ = ("_dispatch_digest", "_dispatched_count", "_queue", "_subscribers")

    def __init__(self, clock: Clock) -> None:
        self._queue = EventQueue(clock)
        self._subscribers: list[_Subscription] = []
        self._dispatch_digest = hashlib.sha256()
        self._dispatched_count = 0

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def dispatch_hash(self) -> str:
        """Return a stable SHA-256 digest of event identities dispatched so far."""
        return self._dispatch_digest.hexdigest()

    @property
    def dispatched_count(self) -> int:
        """Return the number of events dispatched by this bus."""
        return self._dispatched_count

    @overload
    def subscribe(
        self, selector: type[EventT], handler: EventHandler[EventT]
    ) -> None: ...

    @overload
    def subscribe(self, selector: EventType, handler: EventHandler[Event]) -> None: ...

    def subscribe(self, selector: EventSelector, handler: Callable[..., None]) -> None:
        """Register a handler; handlers run in registration order."""
        if not isinstance(selector, (type, EventType)):
            raise TypeError("selector must be an Event class or EventType")
        if isinstance(selector, type) and not issubclass(selector, Event):
            raise TypeError("selector must be an Event class or EventType")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._subscribers.append(
            _Subscription(selector, cast(EventHandler[Event], handler))
        )

    def publish(self, event: Event) -> None:
        """Publish one feed event to the deterministic queue."""
        self._queue.enqueue(event)

    def publish_many(self, events: Iterable[Event]) -> None:
        """Publish events in their incoming feed order."""
        self._queue.enqueue_many(events)

    def schedule(self, response: Event) -> None:
        """Schedule a venue response at its explicit event timestamp."""
        self._queue.enqueue(response)

    def dispatch_next(self) -> Event | None:
        """Dispatch one event and return it, or return ``None`` if none is due."""
        event = self._queue.pop()
        if event is None:
            return None
        for subscription in tuple(self._subscribers):
            subscription.dispatch(event)
        self._record_dispatch(event)
        return event

    def drain(self, limit: int | None = None) -> tuple[Event, ...]:
        """Dispatch due events until empty or until ``limit`` is reached."""
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")
        dispatched: list[Event] = []
        while limit is None or len(dispatched) < limit:
            event = self.dispatch_next()
            if event is None:
                break
            dispatched.append(event)
        return tuple(dispatched)

    def _record_dispatch(self, event: Event) -> None:
        identity = "\0".join(
            (
                f"{type(event).__module__}.{type(event).__qualname__}",
                event.event_type.value,
                event.timestamp.isoformat(timespec="microseconds"),
                str(event.sequence),
                event.event_id,
            )
        ).encode()
        self._dispatch_digest.update(len(identity).to_bytes(8, "big"))
        self._dispatch_digest.update(identity)
        self._dispatched_count += 1


__all__ = [
    "EVENT_CLASS_PRIORITY",
    "EventBus",
    "EventHandler",
    "EventQueue",
]
