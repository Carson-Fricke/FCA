"""Cosmos DB-backed durable persistence for workspace chat records.

Implements the ``ChatPersistence`` port so the chat service, chats, messages,
executions, attachments, investigations, and tool jobs persist in the
``catalog`` container (partitioned by ``workspaceId``) instead of local files.
The record payload format is identical to the filesystem store, so the two are
interchangeable and the rest of the service is unchanged.

When a timeline is supplied, newly created messages and executions are also
appended to the unified per-chat timeline, so one stream can carry the whole
conversation (messages and machine events) in one order.
"""

from __future__ import annotations

from typing import TypeVar, cast

from azure.cosmos import ContainerProxy

from thesis.chat.contracts import ChatMessage, WorkflowExecution
from thesis.chat.persistence import (
    Record,
    RecordType,
    ensure_supported_record_type,
    record_from_payload,
    record_identity,
    record_payload,
)
from thesis.cosmos.timeline import (
    KIND_EXECUTION,
    KIND_MESSAGE,
    ROOT_THREAD,
    CosmosTimeline,
)

RecordT = TypeVar("RecordT", bound=Record)


class CosmosChatPersistence:
    """Durable chat-record persistence in one Cosmos container."""

    def __init__(
        self, container: ContainerProxy, *, timeline: CosmosTimeline | None = None
    ) -> None:
        self._container = container
        self._timeline = timeline

    @staticmethod
    def _item_id(record_type: RecordType, record_id: str) -> str:
        return f"{record_type.__name__}:{record_id}"

    def save(self, record: RecordT) -> RecordT:
        record_type = cast(RecordType, type(record))
        ensure_supported_record_type(record_type)
        self._container.upsert_item(
            {
                "id": self._item_id(record_type, record_identity(record)),
                "workspaceId": record.workspace_id,
                "recordType": record_type.__name__,
                "createdAt": record.created_at.isoformat(),
                "payload": record_payload(record),
            }
        )
        self._mirror_to_timeline(record)
        return record

    def _mirror_to_timeline(self, record: Record) -> None:
        """Append the first snapshot of a message/execution to the timeline."""
        timeline = self._timeline
        if timeline is None:
            return
        if isinstance(record, ChatMessage):
            item_id = f"message:{record.message_id}"
            if timeline.get(chat_id=record.chat_id, item_id=item_id) is not None:
                return
            sub_agent_id = (
                None if record.data is None else record.data.get("subAgentId")
            )
            thread_id = (
                str(sub_agent_id) if isinstance(sub_agent_id, str) else ROOT_THREAD
            )
            timeline.append(
                chat_id=record.chat_id,
                workspace_id=record.workspace_id,
                kind=KIND_MESSAGE,
                payload=record.as_dict(),
                item_id=item_id,
                execution_id=record.execution_id,
                thread_id=thread_id,
                occurred_at=record.created_at.isoformat(),
            )
        elif isinstance(record, WorkflowExecution):
            item_id = f"execution:{record.execution_id}"
            if timeline.get(chat_id=record.chat_id, item_id=item_id) is not None:
                return
            timeline.append(
                chat_id=record.chat_id,
                workspace_id=record.workspace_id,
                kind=KIND_EXECUTION,
                payload=record.as_dict(),
                item_id=item_id,
                execution_id=record.execution_id,
                thread_id=ROOT_THREAD,
                occurred_at=record.created_at.isoformat(),
            )

    def chat_id_for_execution(self, execution_id: str) -> str:
        """Resolve the chat that owns one execution, for timeline partitioning."""
        items = list(
            self._container.query_items(
                query=(
                    "SELECT VALUE c.payload.chat_id FROM c "
                    "WHERE c.recordType = @type AND c.id = @id"
                ),
                parameters=[
                    {"name": "@type", "value": "WorkflowExecution"},
                    {"name": "@id", "value": f"WorkflowExecution:{execution_id}"},
                ],
                enable_cross_partition_query=True,
            )
        )
        if not items or not isinstance(items[0], str):
            raise KeyError(f"unknown execution {execution_id!r}")
        return items[0]

    def get(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> RecordT | None:
        typed = cast(RecordType, record_type)
        ensure_supported_record_type(typed)
        try:
            item = self._container.read_item(
                item=self._item_id(typed, record_id), partition_key=workspace_id
            )
        except Exception:  # noqa: BLE001 - missing or cross-partition reads
            return None
        record = record_from_payload(typed, item["payload"])
        if record.workspace_id != workspace_id or record_identity(record) != record_id:
            return None
        return cast(RecordT, record)

    def list(
        self, record_type: type[RecordT], workspace_id: str
    ) -> tuple[RecordT, ...]:
        typed = cast(RecordType, record_type)
        ensure_supported_record_type(typed)
        items = list(
            self._container.query_items(
                query=(
                    "SELECT * FROM c WHERE c.workspaceId = @ws AND c.recordType = @type"
                ),
                parameters=[
                    {"name": "@ws", "value": workspace_id},
                    {"name": "@type", "value": typed.__name__},
                ],
                partition_key=workspace_id,
            )
        )
        records = [record_from_payload(typed, item["payload"]) for item in items]
        records.sort(key=lambda record: (record.created_at, record_identity(record)))
        return cast(tuple[RecordT, ...], tuple(records))

    def delete(
        self, record_type: RecordType, workspace_id: str, record_id: str
    ) -> None:
        try:
            self._container.delete_item(
                item=self._item_id(record_type, record_id), partition_key=workspace_id
            )
        except Exception:  # noqa: BLE001 - deleting a missing record is a no-op
            return


__all__ = ["CosmosChatPersistence"]
