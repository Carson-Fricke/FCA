"""Ordered, durable execution-event contracts and replay semantics.

The event store is deliberately a small injected port.  A production adapter
can use the workspace event files or another durable store without changing
the UI-facing event shape.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast


class ActorScope(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    WORKFLOW = "workflow"


class ExecutionEventType(StrEnum):
    WORKFLOW_LAUNCHED = "workflow.launched"
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_PHASE = "workflow.phase"
    # Retained to decode historical execution events. Nothing appends it: the
    # workflow engine no longer pauses an execution for operator input.
    WORKFLOW_PAUSED = "workflow.paused"
    WORKFLOW_STEERED = "workflow.steered"
    WORKFLOW_STOPPED = "workflow.stopped"
    WORKFLOW_CANCELLED = "workflow.cancelled"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    MESSAGE_ACCEPTED = "message.accepted"
    TOOL_SUBMITTED = "tool.submitted"
    TOOL_STARTED = "tool.started"
    TOOL_RESULT = "tool.result"
    TOOL_FAILED = "tool.failed"
    SUB_AGENT_SPAWNED = "sub_agent.spawned"
    SUB_AGENT_PROGRESS = "sub_agent.progress"
    SUB_AGENT_OUTPUT = "sub_agent.output"
    SUB_AGENT_NEEDS_INPUT = "sub_agent.needs_input"
    SUB_AGENT_COMPLETED = "sub_agent.completed"
    SUB_AGENT_FAILED = "sub_agent.failed"
    WORKFLOW_GATE_BLOCKED = "workflow.gate.blocked"
    WORKFLOW_GATE_RESOLVED = "workflow.gate.resolved"


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """One append-only event in an execution's ordered stream."""

    execution_id: str
    workspace_id: str
    event_type: ExecutionEventType | str
    correlation_id: str
    actor_id: str
    actor_scope: ActorScope | str
    occurred_at: datetime
    payload: Mapping[str, object] = field(default_factory=dict)
    intent_epoch: int = 0
    late_result: bool = False
    event_id: str = field(default_factory=lambda: f"evt-{uuid.uuid4().hex}")
    cursor: int = 0

    def __post_init__(self) -> None:
        for name in (
            "execution_id",
            "workspace_id",
            "correlation_id",
            "actor_id",
            "event_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if not isinstance(self.event_type, ExecutionEventType):
            object.__setattr__(self, "event_type", ExecutionEventType(self.event_type))
        if not isinstance(self.actor_scope, ActorScope):
            object.__setattr__(self, "actor_scope", ActorScope(self.actor_scope))
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.cursor < 0:
            raise ValueError("cursor must not be negative")
        if self.intent_epoch < 0:
            raise ValueError("intent_epoch must not be negative")
        object.__setattr__(self, "payload", dict(self.payload))

    @property
    def type(self) -> ExecutionEventType:
        """Alias useful to clients that call the discriminator ``type``."""
        return cast(ExecutionEventType, self.event_type)

    def as_dict(self) -> dict[str, object]:
        actor_scope = cast(ActorScope, self.actor_scope)
        event_type = cast(ExecutionEventType, self.event_type)
        return {
            "actorId": self.actor_id,
            "actorScope": actor_scope.value,
            "correlationId": self.correlation_id,
            "cursor": self.cursor,
            "eventId": self.event_id,
            "eventType": event_type.value,
            "executionId": self.execution_id,
            "intentEpoch": self.intent_epoch,
            "lateResult": self.late_result,
            "occurredAt": self.occurred_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "payload": dict(self.payload),
            "workspaceId": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class EventReplay:
    """A reconnect page whose cursor is exclusive for the next request."""

    execution_id: str
    after_cursor: int
    events: tuple[ExecutionEvent, ...]
    next_cursor: int
    has_more: bool = False

    def __iter__(self) -> Iterator[ExecutionEvent]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def __getitem__(self, index: int) -> ExecutionEvent:
        return self.events[index]

    @property
    def cursor(self) -> int:
        """The latest cursor delivered by this page."""
        return self.next_cursor

    def as_dict(self) -> dict[str, object]:
        return {
            "executionId": self.execution_id,
            "afterCursor": self.after_cursor,
            "events": [event.as_dict() for event in self.events],
            "nextCursor": self.next_cursor,
            "hasMore": self.has_more,
        }


class ExecutionEventStore(Protocol):
    """Persistence boundary for append-only execution events."""

    def append(self, event: ExecutionEvent) -> ExecutionEvent: ...

    def replay(
        self,
        execution_id: str,
        *,
        after_cursor: int = 0,
        limit: int | None = None,
    ) -> EventReplay: ...

    def latest_cursor(self, execution_id: str) -> int: ...


class InMemoryExecutionEventStore:
    """Deterministic event-store adapter for local use and contract tests."""

    def __init__(self, events: Iterable[ExecutionEvent] = ()) -> None:
        self._events: dict[str, list[ExecutionEvent]] = {}
        for event in events:
            self.append(event)

    def append(self, event: ExecutionEvent) -> ExecutionEvent:
        stream = self._events.setdefault(event.execution_id, [])
        if stream and stream[0].workspace_id != event.workspace_id:
            raise ValueError("an execution event stream cannot cross workspaces")
        existing = next(
            (item for item in stream if item.event_id == event.event_id), None
        )
        if existing is not None:
            if replace(event, cursor=existing.cursor) != existing:
                raise ValueError("event ID was reused with different content")
            return existing
        if event.cursor not in {0, len(stream) + 1}:
            raise ValueError(
                "event cursor must be assigned by the next stream position"
            )
        assigned = replace(event, cursor=len(stream) + 1)
        stream.append(assigned)
        return assigned

    def replay(
        self,
        execution_id: str,
        *,
        after_cursor: int = 0,
        limit: int | None = None,
    ) -> EventReplay:
        if after_cursor < 0:
            raise ValueError("after_cursor must not be negative")
        if limit is not None and (limit < 1):
            raise ValueError("limit must be positive or None")
        all_events = tuple(self._events.get(execution_id, ()))
        selected = tuple(event for event in all_events if event.cursor > after_cursor)
        has_more = limit is not None and len(selected) > limit
        if limit is not None:
            selected = selected[:limit]
        next_cursor = after_cursor if not selected else selected[-1].cursor
        return EventReplay(
            execution_id=execution_id,
            after_cursor=after_cursor,
            events=selected,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def latest_cursor(self, execution_id: str) -> int:
        stream = self._events.get(execution_id, ())
        return 0 if not stream else stream[-1].cursor


__all__ = [
    "ActorScope",
    "EventReplay",
    "ExecutionEvent",
    "ExecutionEventStore",
    "ExecutionEventType",
    "InMemoryExecutionEventStore",
]
