"""Execution events on the same sequenced conversation log."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, cast

from thesis_runtime.execution.events import (
    ActorScope,
    EventReplay,
    ExecutionEvent,
    ExecutionEventType,
)

from .timeline import PostgresTimeline


def decode_event(data: Mapping[str, Any]) -> ExecutionEvent:
    return ExecutionEvent(
        execution_id=data["executionId"],
        workspace_id=data["workspaceId"],
        event_type=ExecutionEventType(data["eventType"]),
        correlation_id=data["correlationId"],
        actor_id=data["actorId"],
        actor_scope=ActorScope(data["actorScope"]),
        occurred_at=datetime.fromisoformat(data["occurredAt"].replace("Z", "+00:00")),
        payload=data["payload"],
        intent_epoch=data["intentEpoch"],
        late_result=data["lateResult"],
        event_id=data["eventId"],
        cursor=data["cursor"],
    )


class PostgresExecutionEventStore:
    def __init__(
        self,
        timeline: PostgresTimeline,
        *,
        scope_for_execution: Callable[[str], tuple[str, str]],
    ) -> None:
        self.timeline, self.scope_for_execution = timeline, scope_for_execution

    def append(self, event: ExecutionEvent) -> ExecutionEvent:
        workspace_id, chat_id = self.scope_for_execution(event.execution_id)
        row = self.timeline.append(
            workspace_id=workspace_id,
            chat_id=chat_id,
            kind="event",
            payload=event.as_dict(),
            item_id="event:" + event.event_id,
            execution_id=event.execution_id,
            thread_id=str(event.payload.get("subAgentId") or "root"),
            occurred_at=event.occurred_at.isoformat(),
        )
        return decode_event(cast(Mapping[str, Any], row["payload"]))

    def replay(
        self, execution_id: str, *, after_cursor: int = 0, limit: int | None = None
    ) -> EventReplay:
        if after_cursor < 0 or (limit is not None and limit < 1):
            raise ValueError("cursor must be nonnegative and limit positive")
        workspace_id, chat_id = self.scope_for_execution(execution_id)
        rows = self.timeline.store.query(
            "workspace_id=%s AND chat_id=%s AND execution_id=%s AND kind='event' AND seq>%s",
            [workspace_id, chat_id, execution_id, after_cursor],
            order="seq",
            limit=None if limit is None else limit + 1,
        )
        more = limit is not None and len(rows) > limit
        events = tuple(
            decode_event(self.timeline.hydrate(row)["payload"]) for row in rows[:limit]
        )
        return EventReplay(
            execution_id,
            after_cursor,
            events,
            events[-1].cursor if events else after_cursor,
            more,
        )

    def latest_cursor(self, execution_id: str) -> int:
        workspace_id, chat_id = self.scope_for_execution(execution_id)
        rows = self.timeline.store.query(
            "workspace_id=%s AND chat_id=%s AND execution_id=%s AND kind='event'",
            [workspace_id, chat_id, execution_id],
            columns="COALESCE(MAX(seq),0)",
        )
        return int(rows[0][0])
