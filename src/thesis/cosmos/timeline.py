"""The authoritative per-chat conversation log in the workspace catalog.

Messages, execution events, and immutable execution snapshots are the same
documents that persist their records: the catalog container (partitioned by
``workspaceId``) is the only conversation store. A per-chat counter allocates
the single monotonically increasing ``seq`` ordering that one SSE stream
carries for the whole conversation, root and every sub-agent. The counter CAS
and item creation share one transactional batch in the workspace partition, so
no sequence is ever reserved independently of its publication.

There is no second timeline container and no projection outbox. Reads scope on
indexed top-level ownership fields (``chatId``, ``threadId``), never on nested
payload predicates.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosBatchOperationError,
    CosmosHttpResponseError,
    CosmosResourceNotFoundError,
)

from thesis.chat.streaming import CommitNotifier
from thesis.cosmos.chunks import MANIFEST_KEY, ChunkedPayloadStore, ensure_bounded_item

#: Timeline item kinds.
KIND_MESSAGE = "message"
KIND_EVENT = "event"
KIND_EXECUTION = "execution"
KIND_COUNTER = "counter"

#: The thread id used for the root agent's own conversation.
ROOT_THREAD = "root"
MAX_APPEND_ATTEMPTS = 32


def counter_id(chat_id: str) -> str:
    """Return the sequence counter's document identity for one chat."""
    return f"counter:{chat_id}"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class CosmosTimeline:
    """Ordered, catalog-backed conversation log."""

    def __init__(
        self,
        container: ContainerProxy,
        *,
        chunks: ChunkedPayloadStore | None = None,
        notifier: CommitNotifier | None = None,
    ) -> None:
        self._container = container
        self._chunks = chunks
        self._notifier = notifier if notifier is not None else CommitNotifier()

    @property
    def container(self) -> ContainerProxy:
        """Return the shared catalog container."""
        return self._container

    @property
    def commits(self) -> CommitNotifier:
        """Return the post-commit wake channel for timeline readers."""
        return self._notifier

    def hydrate(self, document: Mapping[str, object]) -> dict[str, object]:
        """Hydrate chunked payloads and restore the read-shape invariants.

        Chunked payloads are replaced with their decoded value. An execution
        exposes its immutable first-save snapshot, while ``get`` on the record
        keeps the mutable current payload. An event payload carries its
        committed sequence as ``cursor``.
        """
        result = dict(document)
        workspace_id = cast(str, result["workspaceId"])
        payload = result.get("payload")
        if isinstance(payload, Mapping) and MANIFEST_KEY in payload:
            if self._chunks is None:
                raise ValueError(
                    "timeline chunk store is required for chunked payloads"
                )
            result["payload"] = self._chunks.load(payload, workspace_id)
        if result.get("kind") == KIND_EXECUTION:
            snapshot = result.get("initialPayload")
            if isinstance(snapshot, Mapping) and MANIFEST_KEY in snapshot:
                if self._chunks is None:
                    raise ValueError(
                        "timeline chunk store is required for chunked payloads"
                    )
                result["payload"] = self._chunks.load(snapshot, workspace_id)
            elif isinstance(snapshot, Mapping):
                result["payload"] = dict(snapshot)
        if result.get("kind") == KIND_EVENT:
            result["payload"] = {
                **cast(dict[str, object], result["payload"]),
                "cursor": result["seq"],
            }
        return result

    def _read_item(
        self, *, workspace_id: str, item_id: str
    ) -> dict[str, object] | None:
        try:
            return dict(
                self._container.read_item(item=item_id, partition_key=workspace_id)
            )
        except CosmosResourceNotFoundError:
            return None

    def latest_sequence(self, *, workspace_id: str, chat_id: str) -> int:
        """Return the chat's newest allocated sequence, or ``0``."""
        counter = self._read_item(
            workspace_id=workspace_id, item_id=counter_id(chat_id)
        )
        return int(cast(int, counter["nextSeq"])) if counter else 0

    def create_with_sequence(
        self,
        document: Mapping[str, object],
        *,
        workspace_id: str,
        chat_id: str,
    ) -> dict[str, object]:
        """Create ``document`` with the chat's next ``seq`` in one batch.

        The counter replace (CAS) and the item create commit together. A
        conflict re-reads the item: a concurrent creator already committed it,
        so the durable snapshot is returned instead of allocating a sequence.
        """
        prepared = dict(document)
        for attempt in range(MAX_APPEND_ATTEMPTS):
            counter = self._read_item(
                workspace_id=workspace_id, item_id=counter_id(chat_id)
            )
            seq = 1 if counter is None else int(cast(int, counter["nextSeq"])) + 1
            prepared["seq"] = seq
            counter_document: dict[str, object] = {
                "id": counter_id(chat_id),
                "workspaceId": workspace_id,
                "chatId": chat_id,
                "nextSeq": seq,
            }
            ensure_bounded_item(prepared)
            ensure_bounded_item(counter_document)
            operations: list[
                tuple[str, tuple[Any, ...]]
                | tuple[str, tuple[Any, ...], dict[str, Any]]
            ] = [
                ("create", (counter_document,))
                if counter is None
                else (
                    "replace",
                    (counter_id(chat_id), counter_document),
                    {"if_match_etag": counter["_etag"]},
                ),
                ("create", (prepared,)),
            ]
            try:
                self._container.execute_item_batch(
                    batch_operations=operations, partition_key=workspace_id
                )
            except (CosmosBatchOperationError, CosmosHttpResponseError) as error:
                if error.status_code not in {409, 412}:
                    raise
                existing = self._read_item(
                    workspace_id=workspace_id, item_id=cast(str, prepared["id"])
                )
                if existing is not None:
                    return existing
                if attempt == MAX_APPEND_ATTEMPTS - 1:
                    raise
            else:
                self._notifier.notify(workspace_id, chat_id)
                return prepared
        raise AssertionError("unreachable append retry exhaustion")

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
    ) -> dict[str, object]:
        """Commit one standalone item (an execution event) with its sequence."""
        if item_id == counter_id(chat_id):
            raise ValueError("counter is reserved for timeline sequencing")
        existing = self._read_item(workspace_id=workspace_id, item_id=item_id)
        if existing is not None:
            return self.hydrate(existing)
        document: dict[str, object] = {
            "id": item_id,
            "workspaceId": workspace_id,
            "kind": kind,
            "chatId": chat_id,
            "ts": occurred_at or _now(),
            "payload": (
                payload
                if self._chunks is None
                else self._chunks.dump(payload, workspace_id)
            ),
        }
        if execution_id is not None:
            document["executionId"] = execution_id
        if thread_id is not None:
            document["threadId"] = thread_id
        return self.hydrate(
            self.create_with_sequence(
                document, workspace_id=workspace_id, chat_id=chat_id
            )
        )

    def read_after(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        after_seq: int = 0,
        limit: int | None = None,
        thread_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Read items with ``seq > after_seq`` in ascending order."""
        query = (
            ("SELECT TOP @limit * FROM c " if limit is not None else "SELECT * FROM c ")
            + "WHERE c.chatId = @chat AND c.seq > @after "
            + ("AND c.threadId = @thread " if thread_id else "")
            + "ORDER BY c.seq ASC"
        )
        parameters: list[dict[str, object]] = [
            {"name": "@chat", "value": chat_id},
            {"name": "@after", "value": after_seq},
        ]
        if thread_id:
            parameters.append({"name": "@thread", "value": thread_id})
        if limit is not None:
            parameters.append({"name": "@limit", "value": limit})
        items = list(
            self._container.query_items(
                query=query,
                parameters=parameters,
                partition_key=workspace_id,
                max_item_count=limit,
            )
        )
        if limit is not None:
            items = items[:limit]
        return [self.hydrate(item) for item in items]

    def read_before(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        before_seq: int,
        limit: int,
        thread_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Return the nearest ``limit`` items with ``seq < before_seq``, ascending."""
        query = (
            "SELECT TOP @limit * FROM c "
            "WHERE c.chatId = @chat AND c.seq < @before "
            + ("AND c.threadId = @thread " if thread_id else "")
            + "ORDER BY c.seq DESC"
        )
        parameters: list[dict[str, object]] = [
            {"name": "@limit", "value": limit},
            {"name": "@chat", "value": chat_id},
            {"name": "@before", "value": before_seq},
        ]
        if thread_id:
            parameters.append({"name": "@thread", "value": thread_id})
        recent = list(
            self._container.query_items(
                query=query,
                parameters=parameters,
                partition_key=workspace_id,
                max_item_count=limit,
            )
        )[:limit]
        recent.reverse()
        return [self.hydrate(item) for item in recent]


__all__ = [
    "KIND_COUNTER",
    "KIND_EVENT",
    "KIND_EXECUTION",
    "KIND_MESSAGE",
    "ROOT_THREAD",
    "CosmosTimeline",
    "counter_id",
]
