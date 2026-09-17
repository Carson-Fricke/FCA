"""Cosmos DB-backed durable execution-event store.

Events are the machine timeline of a conversation and now persist in the same
``timeline`` container as messages, partitioned by ``chatId`` and ordered by the
chat's ``seq``.  The store keeps the existing ``ExecutionEventStore`` contract
(``append`` / ``replay`` / ``latest_cursor``) so the workflow and chat service
are unchanged, while the shared ``seq`` is what lets one stream merge messages
and events.

The event's per-execution ``cursor`` is the chat-wide ``seq``: it is monotonic
and resumable, which is all the SSE replay protocol requires.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from azure.cosmos import ContainerProxy

from thesis.cosmos.timeline import KIND_EVENT, ROOT_THREAD, CosmosTimeline
from thesis.execution.events import (
    ActorScope,
    EventReplay,
    ExecutionEvent,
    ExecutionEventType,
)


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
    """Durable event store over the shared per-chat Cosmos timeline."""

    def __init__(
        self,
        timeline: CosmosTimeline,
        *,
        chat_for_execution: Callable[[str], str],
    ) -> None:
        self._timeline = timeline
        self._container: ContainerProxy = timeline.container
        self._chat_for_execution = chat_for_execution
        self._chat_cache: dict[str, str] = {}

    def append(self, event: ExecutionEvent) -> ExecutionEvent:
        chat_id = self._chat_id(event.execution_id)
        sub_agent_id = event.payload.get("subAgentId")
        thread_id = str(sub_agent_id) if isinstance(sub_agent_id, str) else ROOT_THREAD
        seq = self._timeline.next_seq(chat_id)
        stored = replace(event, cursor=seq)
        self._timeline.append(
            chat_id=chat_id,
            workspace_id=event.workspace_id,
            kind=KIND_EVENT,
            payload=stored.as_dict(),
            item_id=f"event:{event.event_id}",
            execution_id=event.execution_id,
            thread_id=thread_id,
            occurred_at=stored.occurred_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            seq=seq,
        )
        return stored

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
        chat_id = self._chat_id(execution_id)
        items = list(
            self._container.query_items(
                query=(
                    "SELECT * FROM c WHERE c.chatId = @chat "
                    "AND c.executionId = @exec AND c.kind = @kind "
                    "AND c.seq > @after ORDER BY c.seq ASC"
                ),
                parameters=[
                    {"name": "@chat", "value": chat_id},
                    {"name": "@exec", "value": execution_id},
                    {"name": "@kind", "value": KIND_EVENT},
                    {"name": "@after", "value": after_cursor},
                ],
                partition_key=chat_id,
            )
        )
        events = tuple(_event_from_dict(item["payload"]) for item in items)
        has_more = limit is not None and len(events) > limit
        if limit is not None:
            events = events[:limit]
        next_cursor = after_cursor if not events else events[-1].cursor
        return EventReplay(
            execution_id=execution_id,
            after_cursor=after_cursor,
            events=events,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def latest_cursor(self, execution_id: str) -> int:
        chat_id = self._chat_id(execution_id)
        items = list(
            self._container.query_items(
                query=(
                    "SELECT VALUE MAX(c.seq) FROM c WHERE c.chatId = @chat "
                    "AND c.executionId = @exec AND c.kind = @kind"
                ),
                parameters=[
                    {"name": "@chat", "value": chat_id},
                    {"name": "@exec", "value": execution_id},
                    {"name": "@kind", "value": KIND_EVENT},
                ],
                partition_key=chat_id,
            )
        )
        return _int(items[0]) if items and items[0] is not None else 0

    def _chat_id(self, execution_id: str) -> str:
        cached = self._chat_cache.get(execution_id)
        if cached is not None:
            return cached
        chat_id = self._chat_for_execution(execution_id)
        self._chat_cache[execution_id] = chat_id
        return chat_id


__all__ = ["CosmosExecutionEventStore"]
