"""Cosmos DB-backed durable execution-event store.

Events are the machine timeline of a conversation and persist in the same
workspace-partitioned ``catalog`` container as every other conversation
document, filtered by ``chatId``/``executionId`` and ordered by the chat's
``seq``.  The store keeps the existing ``ExecutionEventStore`` contract
(``append`` / ``replay`` / ``latest_cursor``) so the workflow and chat service
are unchanged, while the shared ``seq`` is what lets one stream merge messages
and events.

The event's per-execution ``cursor`` is the chat-wide ``seq``: it is monotonic
and resumable, which is all the SSE replay protocol requires.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from azure.cosmos import ContainerProxy

from thesis_runtime.adapters.cosmos.timeline import (
    KIND_EVENT,
    ROOT_THREAD,
    CosmosTimeline,
)
from thesis_runtime.execution.events import (
    ActorScope,
    EventReplay,
    ExecutionEvent,
    ExecutionEventType,
)
from thesis_runtime.storage.locks import conversation_lock


def _int(value: object, default: int = 0) -> int:
    """Coerce a Cosmos scalar to ``int`` with a safe fallback."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _event_from_dict(data: dict[str, object]) -> ExecutionEvent:
    payload = data.get("payload")
    return ExecutionEvent(
        execution_id=str(data["executionId"]),
        workspace_id=str(data["workspaceId"]),
        event_type=ExecutionEventType(str(data["eventType"])),
        correlation_id=str(data["correlationId"]),
        actor_id=str(data["actorId"]),
        actor_scope=ActorScope(str(data["actorScope"])),
        occurred_at=datetime.fromisoformat(
            str(data["occurredAt"]).replace("Z", "+00:00")
        ).astimezone(UTC),
        payload=dict(payload) if isinstance(payload, dict) else {},
        intent_epoch=_int(data.get("intentEpoch")),
        late_result=bool(data.get("lateResult", False)),
        event_id=str(data["eventId"]),
        cursor=_int(data.get("cursor")),
    )


class CosmosExecutionEventStore:
    """Durable event store over the shared workspace catalog."""

    def __init__(
        self,
        timeline: CosmosTimeline,
        *,
        scope_for_execution: Callable[[str], tuple[str, str]],
        require_active_conversation: Callable[[str, str], None] | None = None,
    ) -> None:
        self._timeline = timeline
        self._container: ContainerProxy = timeline.container
        self._scope_for_execution = scope_for_execution
        self._require_active_conversation = require_active_conversation
        self._scope_cache: dict[str, tuple[str, str]] = {}

    def append(self, event: ExecutionEvent) -> ExecutionEvent:
        workspace_id, chat_id = self._scope(event.execution_id)
        with conversation_lock("cosmos", workspace_id, chat_id):
            if self._require_active_conversation is not None:
                self._require_active_conversation(workspace_id, chat_id)
            return self._append(event, workspace_id, chat_id)

    def _append(
        self, event: ExecutionEvent, workspace_id: str, chat_id: str
    ) -> ExecutionEvent:
        sub_agent_id = event.payload.get("subAgentId")
        thread_id = str(sub_agent_id) if isinstance(sub_agent_id, str) else ROOT_THREAD
        stored = self._timeline.append(
            chat_id=chat_id,
            workspace_id=workspace_id,
            kind=KIND_EVENT,
            payload=event.as_dict(),
            item_id=f"event:{event.event_id}",
            execution_id=event.execution_id,
            thread_id=thread_id,
            occurred_at=event.occurred_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
        )
        return _event_from_dict(cast(dict[str, object], stored["payload"]))

    def replay(
        self,
        execution_id: str,
        *,
        after_cursor: int = 0,
        limit: int | None = None,
    ) -> EventReplay:
        if after_cursor < 0:
            raise ValueError("after_cursor must not be negative")
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive or None")
        workspace_id, chat_id = self._scope(execution_id)
        parameters: list[dict[str, object]] = [
            {"name": "@ws", "value": workspace_id},
            {"name": "@chat", "value": chat_id},
            {"name": "@exec", "value": execution_id},
            {"name": "@kind", "value": KIND_EVENT},
            {"name": "@after", "value": after_cursor},
        ]
        if limit is not None:
            parameters.append({"name": "@limit", "value": limit + 1})
        items = list(
            self._container.query_items(
                query=(
                    (
                        "SELECT TOP @limit * FROM c "
                        if limit is not None
                        else "SELECT * FROM c "
                    )
                    + "WHERE c.workspaceId = @ws AND c.chatId = @chat "
                    "AND c.executionId = @exec AND c.kind = @kind "
                    "AND c.seq > @after ORDER BY c.seq ASC"
                ),
                parameters=parameters,
                partition_key=workspace_id,
                max_item_count=None if limit is None else limit + 1,
            )
        )
        has_more = limit is not None and len(items) > limit
        if limit is not None:
            items = items[:limit]
        events = tuple(
            _event_from_dict(
                cast(dict[str, object], self._timeline.hydrate(item)["payload"])
            )
            for item in items
        )
        next_cursor = after_cursor if not events else events[-1].cursor
        return EventReplay(
            execution_id=execution_id,
            after_cursor=after_cursor,
            events=events,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def latest_cursor(self, execution_id: str) -> int:
        workspace_id, chat_id = self._scope(execution_id)
        items = list(
            self._container.query_items(
                query=(
                    "SELECT VALUE MAX(c.seq) FROM c WHERE c.workspaceId = @ws "
                    "AND c.chatId = @chat AND c.executionId = @exec AND c.kind = @kind"
                ),
                parameters=[
                    {"name": "@ws", "value": workspace_id},
                    {"name": "@chat", "value": chat_id},
                    {"name": "@exec", "value": execution_id},
                    {"name": "@kind", "value": KIND_EVENT},
                ],
                partition_key=workspace_id,
            )
        )
        return _int(items[0]) if items and items[0] is not None else 0

    def _scope(self, execution_id: str) -> tuple[str, str]:
        cached = self._scope_cache.get(execution_id)
        if cached is not None:
            return cached
        scope = self._scope_for_execution(execution_id)
        self._scope_cache[execution_id] = scope
        return scope


__all__ = ["CosmosExecutionEventStore"]
