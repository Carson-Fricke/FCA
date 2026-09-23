"""Cosmos DB-backed durable execution store (REQ-B5).

One document per workflow execution in the ``executions`` container,
partitioned by ``executionId`` so an execution's whole history — identity,
launch request, ordered transitions with their completion contracts, and the
per-phase result artifacts — is a single point-readable document whose
lifecycle is independent of chat deletion.

Optimistic concurrency is two-layered: the stored document's ``revision`` is
checked against the caller's ``expected_revision`` before a write, and the
Cosmos ETag pins the read-modify-write itself, so a concurrent writer turns
into :class:`ExecutionStoreConflict` instead of a silent clobber.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from azure.core import MatchConditions
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from thesis.workflows.core.execution_store import (
    ExecutionDocument,
    ExecutionIdentity,
    ExecutionStoreConflict,
    ExecutionStoreError,
    ExecutionStoreNotFound,
    StoreTransition,
)

#: The container that holds the execution-store documents.
EXECUTION_STORE_CONTAINER = "executions"

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
            self._container.create_item(self._envelope(document))
        except CosmosResourceExistsError as error:
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
        try:
            return dict(
                self._container.read_item(
                    item=_item_id(execution_id), partition_key=execution_id
                )
            )
        except CosmosResourceNotFoundError as error:
            raise ExecutionStoreNotFound(
                f"execution {execution_id!r} was not found"
            ) from error

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
        stored["payload"] = document.as_dict()
        etag = envelope.get("_etag")
        try:
            if etag is not None:
                self._container.replace_item(
                    item=stored["id"],
                    body=stored,
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                )
            else:
                self._container.replace_item(item=stored["id"], body=stored)
        except CosmosAccessConditionFailedError as error:
            raise ExecutionStoreConflict(
                f"execution {document.execution_id!r} was modified concurrently"
            ) from error
        except CosmosResourceNotFoundError as error:
            raise ExecutionStoreNotFound(
                f"execution {document.execution_id!r} was not found"
            ) from error
        return document

    @staticmethod
    def _envelope(document: ExecutionDocument) -> dict[str, object]:
        return {
            "id": _item_id(document.execution_id),
            "executionId": document.execution_id,
            "workspaceId": document.identity.workspace_id,
            "chatId": document.identity.chat_id,
            "recordType": RECORD_TYPE,
            "createdAt": _now(),
            "payload": document.as_dict(),
        }


__all__ = [
    "EXECUTION_STORE_CONTAINER",
    "RECORD_TYPE",
    "CosmosExecutionStore",
]
