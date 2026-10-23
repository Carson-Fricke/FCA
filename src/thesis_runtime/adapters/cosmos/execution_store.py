"""Workspace-partitioned workflow execution documents and phase artifacts.

Revision CAS and owner fences commit in the catalog transaction. Execution IDs
resolve to workspace scopes through an indexed lookup; the local scope map is
only a cache. Immutable chunk trees share the same workspace partition.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from azure.core.exceptions import HttpResponseError
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosResourceNotFoundError,
)

from thesis_runtime.adapters.cosmos.chunks import (
    ChunkedPayloadStore,
    ensure_bounded_item,
)
from thesis_runtime.adapters.cosmos.ownership import fenced_batch
from thesis_runtime.workflows.execution_store import (
    ExecutionDocument,
    ExecutionIdentity,
    ExecutionStoreConflict,
    ExecutionStoreError,
    ExecutionStoreNotFound,
    StoreTransition,
)

#: The container that holds the execution-store documents.
EXECUTION_STORE_CONTAINER = "catalog"

#: Catalog-style discriminator for an execution-store document.
RECORD_TYPE = "ExecutionDocument"


def _item_id(execution_id: str) -> str:
    return f"execution-store:{execution_id}"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _phase_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionStoreError("phase must be non-empty text")
    return value.strip()


class CosmosExecutionStore:
    """The execution store over one Cosmos container (one doc per execution)."""

    def __init__(self, container: ContainerProxy) -> None:
        self._container = container
        self._chunks = ChunkedPayloadStore(container)
        self._scopes: dict[str, str] = {}

    def identities(self) -> Iterator[ExecutionIdentity]:
        """Bounded metadata pages; defer launch hydration until each candidate."""
        cursor = ""
        while True:
            rows = list(
                self._container.query_items(
                    query="SELECT TOP 64 c.id, c.workspaceId FROM c WHERE c.recordType = @type AND c.id > @after ORDER BY c.id ASC",
                    parameters=[
                        {"name": "@type", "value": RECORD_TYPE},
                        {"name": "@after", "value": cursor},
                    ],
                    enable_cross_partition_query=True,
                    max_item_count=64,
                )
            )
            if not rows:
                return
            for row in rows:
                cursor = row["id"]
                execution_id = cursor.removeprefix("execution-store:")
                self._scopes[execution_id] = row["workspaceId"]
                yield self.get(execution_id).identity

    def create(
        self,
        identity: ExecutionIdentity,
        *,
        launch_request: Mapping[str, object] | None = None,
    ) -> ExecutionDocument:
        document = ExecutionDocument(
            identity=identity,
            launch_request=dict(launch_request or {}),
        )
        try:
            fenced_batch(
                self._container,
                identity.workspace_id,
                [("create", (self._envelope(document),))],
            )
            self._scopes[identity.execution_id] = identity.workspace_id
        except HttpResponseError as error:
            if error.status_code != 409:
                raise
            raise ExecutionStoreConflict(
                f"execution {identity.execution_id!r} already exists"
            ) from error
        return document

    def get(self, execution_id: str) -> ExecutionDocument:
        return ExecutionDocument.from_dict(self._read(execution_id)["payload"])

    def append_transition(
        self,
        execution_id: str,
        transition: StoreTransition,
        *,
        expected_revision: int | None = None,
    ) -> ExecutionDocument:
        envelope = self._checked(execution_id, expected_revision)
        document = ExecutionDocument.from_dict(envelope["payload"])
        if transition.sequence != len(document.transitions) + 1:
            raise ExecutionStoreError(
                f"transition sequence {transition.sequence} does not follow "
                f"{len(document.transitions)} stored transition(s)"
            )
        document = replace(
            document,
            transitions=(*document.transitions, transition),
            revision=document.revision + 1,
        )
        return self._write(envelope, document)

    def put_phase_result(
        self,
        execution_id: str,
        phase: str,
        artifact: Mapping[str, object],
        *,
        expected_revision: int | None = None,
    ) -> ExecutionDocument:
        envelope = self._checked(execution_id, expected_revision)
        document = ExecutionDocument.from_dict(envelope["payload"])
        if not isinstance(artifact, Mapping):
            raise ExecutionStoreError("artifact must be a mapping")
        results = dict(document.phase_results)
        results[_phase_name(phase)] = dict(artifact)
        document = replace(
            document,
            phase_results=results,
            revision=document.revision + 1,
        )
        return self._write(envelope, document)

    # -- internals ------------------------------------------------------------

    def _read(self, execution_id: str) -> dict[str, Any]:
        workspace_id = self._scopes.get(execution_id)
        if workspace_id is None:
            matches = list(
                self._container.query_items(
                    query="SELECT c.workspaceId FROM c WHERE c.id = @id AND c.recordType = 'ExecutionDocument'",
                    parameters=[{"name": "@id", "value": _item_id(execution_id)}],
                    enable_cross_partition_query=True,
                )
            )
            if len(matches) != 1:
                raise ExecutionStoreNotFound(
                    f"execution {execution_id!r} was not found"
                )
            workspace_id = str(matches[0]["workspaceId"])
            self._scopes[execution_id] = workspace_id
        try:
            envelope = dict(
                self._container.read_item(
                    item=_item_id(execution_id), partition_key=workspace_id
                )
            )
        except CosmosResourceNotFoundError as error:
            raise ExecutionStoreNotFound(
                f"execution {execution_id!r} was not found"
            ) from error
        envelope["payload"] = self._chunks.load(envelope["payload"], workspace_id)
        return envelope

    def _checked(
        self, execution_id: str, expected_revision: int | None
    ) -> dict[str, Any]:
        envelope = self._read(execution_id)
        if expected_revision is not None:
            stored = ExecutionDocument.from_dict(envelope["payload"])
            if expected_revision != stored.revision:
                raise ExecutionStoreConflict(
                    f"execution {execution_id!r} is at revision {stored.revision}, "
                    f"not the expected revision {expected_revision}"
                )
        return envelope

    def _write(
        self, envelope: dict[str, Any], document: ExecutionDocument
    ) -> ExecutionDocument:
        stored = dict(envelope)
        stored["payload"] = self._chunks.dump(
            document.as_dict(), document.identity.workspace_id
        )
        ensure_bounded_item(stored)
        etag = envelope.get("_etag")
        try:
            operation = ("replace", (stored["id"], stored), {"if_match_etag": etag})
            fenced_batch(self._container, document.identity.workspace_id, [operation])
        except HttpResponseError as error:
            if error.status_code == 404:
                raise ExecutionStoreNotFound(
                    f"execution {document.execution_id!r} was not found"
                ) from error
            if error.status_code != 412:
                raise
            raise ExecutionStoreConflict(
                f"execution {document.execution_id!r} was modified concurrently"
            ) from error
        except CosmosResourceNotFoundError as error:
            raise ExecutionStoreNotFound(
                f"execution {document.execution_id!r} was not found"
            ) from error
        return document

    def _envelope(self, document: ExecutionDocument) -> dict[str, object]:
        envelope = {
            "id": _item_id(document.execution_id),
            "executionId": document.execution_id,
            "workspaceId": document.identity.workspace_id,
            "chatId": document.identity.chat_id,
            "recordType": RECORD_TYPE,
            "createdAt": _now(),
            "payload": self._chunks.dump(
                document.as_dict(), document.identity.workspace_id
            ),
        }
        ensure_bounded_item(envelope)
        return envelope


__all__ = [
    "EXECUTION_STORE_CONTAINER",
    "RECORD_TYPE",
    "CosmosExecutionStore",
]
