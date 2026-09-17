"""The unified per-chat conversation timeline in Cosmos DB.

Every chat-scoped item — user/assistant messages, tool messages, execution
events, execution state changes — is one document in the ``timeline`` container,
partitioned by ``chatId`` and ordered by a monotonically increasing ``seq``.
That single ordering is what lets one SSE stream carry the whole conversation
(root and every sub-agent) and lets a view filter by ``threadId`` without a
second connection.

Cosmos has no auto-increment, so ``seq`` is issued by a small ``counter`` item
per chat using ETag optimistic concurrency.  A chat is written by one operator
and one workflow at a time, so the retry loop converges immediately.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

#: Item kinds on the timeline.
KIND_MESSAGE = "message"
KIND_EVENT = "event"
KIND_EXECUTION = "execution"
KIND_CHAT = "chat"
KIND_COUNTER = "counter"

#: The thread id used for the root agent's own conversation.
ROOT_THREAD = "root"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class CosmosTimeline:
    """Append-only, per-chat ordered log over one Cosmos container."""

    def __init__(self, container: ContainerProxy) -> None:
        self._container = container

    @property
    def container(self) -> ContainerProxy:
        """Return the underlying Cosmos container."""
        return self._container

    def get(self, *, chat_id: str, item_id: str) -> dict[str, object] | None:
        """Return one timeline item, or ``None`` when it does not exist."""
        try:
            return self._container.read_item(item=item_id, partition_key=chat_id)
        except CosmosResourceNotFoundError:
            return None

    def append(
        self,
        *,
        chat_id: str,
        workspace_id: str,
        kind: str,
        payload: Mapping[str, object],
        item_id: str,
        execution_id: str | None = None,
        thread_id: str | None = None,
        occurred_at: str | None = None,
        seq: int | None = None,
    ) -> dict[str, object]:
        """Append one timeline item and return the stored document."""
        document: dict[str, object] = {
            "id": item_id,
            "chatId": chat_id,
            "workspaceId": workspace_id,
            "kind": kind,
            "seq": self.next_seq(chat_id) if seq is None else seq,
            "ts": occurred_at or _now(),
            "payload": dict(payload),
        }
        if execution_id is not None:
            document["executionId"] = execution_id
        if thread_id is not None:
            document["threadId"] = thread_id
        self._container.upsert_item(document)
        return document

    def next_seq(self, chat_id: str) -> int:
        """Atomically issue the next sequence number for one chat."""
        counter_id = "counter"
        try:
            self._container.read_item(item=counter_id, partition_key=chat_id)
        except CosmosResourceNotFoundError:
            try:
                self._container.create_item(
                    {
                        "id": counter_id,
                        "chatId": chat_id,
                        "kind": KIND_COUNTER,
                        "nextSeq": 0,
                    }
                )
            except CosmosResourceExistsError:
                pass
        # Partial document update with ``incr`` is atomic, so no ETag loop is
        # needed and concurrent appends cannot collide.
        updated = self._container.patch_item(
            item=counter_id,
            partition_key=chat_id,
            patch_operations=[{"op": "incr", "path": "/nextSeq", "value": 1}],
        )
        return int(updated["nextSeq"])

    def read_after(
        self,
        *,
        chat_id: str,
        after_seq: int = 0,
        limit: int | None = None,
        thread_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Read items with ``seq > after_seq`` in ascending order."""
        query = (
            "SELECT * FROM c WHERE c.chatId = @chat AND c.seq > @after "
            + ("AND c.threadId = @thread " if thread_id else "")
            + "ORDER BY c.seq ASC"
        )
        parameters: list[dict[str, object]] = [
            {"name": "@chat", "value": chat_id},
            {"name": "@after", "value": after_seq},
        ]
        if thread_id:
            parameters.append({"name": "@thread", "value": thread_id})
        items = list(
            self._container.query_items(
                query=query,
                parameters=parameters,
                partition_key=chat_id,
                max_item_count=limit,
            )
        )
        if limit is not None:
            items = items[:limit]
        return items

    def last(
        self,
        *,
        chat_id: str,
        limit: int,
        thread_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Return the most recent ``limit`` items in ascending order."""
        query = (
            "SELECT * FROM c WHERE c.chatId = @chat AND c.kind <> @counter "
            + ("AND c.threadId = @thread " if thread_id else "")
            + "ORDER BY c.seq DESC"
        )
        parameters: list[dict[str, object]] = [
            {"name": "@chat", "value": chat_id},
            {"name": "@counter", "value": KIND_COUNTER},
        ]
        if thread_id:
            parameters.append({"name": "@thread", "value": thread_id})
        recent = list(
            self._container.query_items(
                query=query,
                parameters=parameters,
                partition_key=chat_id,
                max_item_count=limit,
            )
        )[:limit]
        recent.reverse()
        return recent


__all__ = [
    "KIND_CHAT",
    "KIND_COUNTER",
    "KIND_EVENT",
    "KIND_EXECUTION",
    "KIND_MESSAGE",
    "ROOT_THREAD",
    "CosmosTimeline",
]
