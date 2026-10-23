"""Workflow documents with revision CAS and metadata-only recovery batches."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from thesis_runtime.workflows.execution_store import (
    ExecutionDocument,
    ExecutionIdentity,
    ExecutionStoreConflict,
    ExecutionStoreError,
    ExecutionStoreNotFound,
    StoreTransition,
)

from .store import DocumentConflict, PostgresStore


class PostgresExecutionStore:
    def __init__(self, store: PostgresStore) -> None:
        self.store = store

    def identities(self) -> Iterator[ExecutionIdentity]:
        # Keyset pages bound memory and database work. Hydrate launches and phase
        # artifacts only after the worker has selected a live execution.
        cursor = ""
        while True:
            rows = self.store.query(
                "record_type='ExecutionDocument' AND id>%s",
                [cursor],
                columns="id,body#>'{payload,identity}'",
                order="id",
                limit=64,
            )
            if not rows:
                return
            for item_id, payload in rows:
                cursor = item_id
                yield ExecutionIdentity.from_dict(payload)

    @staticmethod
    def _envelope(document: ExecutionDocument) -> dict[str, Any]:
        return {
            "id": "execution-store:" + document.execution_id,
            "executionId": document.execution_id,
            "workspaceId": document.identity.workspace_id,
            "chatId": document.identity.chat_id,
            "recordType": "ExecutionDocument",
            "createdAt": datetime.now(UTC).isoformat(),
            "payload": document.as_dict(),
        }

    def create(
        self,
        identity: ExecutionIdentity,
        *,
        launch_request: Mapping[str, object] | None = None,
    ) -> ExecutionDocument:
        document = ExecutionDocument(
            identity=identity, launch_request=dict(launch_request or {})
        )
        try:
            with self.store.transaction(
                identity.workspace_id, chat_id=identity.chat_id
            ) as connection:
                self.store.put(connection, self._envelope(document), create=True)
        except DocumentConflict as error:
            raise ExecutionStoreConflict(
                "execution already exists: " + identity.execution_id
            ) from error
        return document

    def _read(self, execution_id: str) -> dict[str, Any]:
        rows = self.store.query(
            "record_type='ExecutionDocument' AND id=%s",
            ["execution-store:" + execution_id],
            columns="workspace_id",
            limit=2,
        )
        if len(rows) != 1:
            raise ExecutionStoreNotFound("unknown execution: " + execution_id)
        document = self.store.read(rows[0][0], "execution-store:" + execution_id)
        if document is None:
            raise ExecutionStoreNotFound("unknown execution: " + execution_id)
        return document

    def get(self, execution_id: str) -> ExecutionDocument:
        return ExecutionDocument.from_dict(self._read(execution_id)["payload"])

    def _change(
        self, execution_id: str, expected_revision: int | None, change: Any
    ) -> ExecutionDocument:
        envelope = self._read(execution_id)
        document = ExecutionDocument.from_dict(envelope["payload"])
        if expected_revision is not None and document.revision != expected_revision:
            raise ExecutionStoreConflict("execution revision changed: " + execution_id)
        updated = change(document)
        try:
            with self.store.transaction(
                document.identity.workspace_id, chat_id=document.identity.chat_id
            ) as connection:
                self.store.put(
                    connection,
                    {**envelope, "payload": updated.as_dict()},
                    expected=envelope["_etag"],
                )
        except DocumentConflict as error:
            raise ExecutionStoreConflict(
                "execution revision changed: " + execution_id
            ) from error
        return updated

    def append_transition(
        self,
        execution_id: str,
        transition: StoreTransition,
        *,
        expected_revision: int | None = None,
    ) -> ExecutionDocument:
        def change(document: ExecutionDocument) -> ExecutionDocument:
            if transition.sequence != len(document.transitions) + 1:
                raise ExecutionStoreError(
                    "transition sequence does not follow stored transitions"
                )
            return replace(
                document,
                transitions=(*document.transitions, transition),
                revision=document.revision + 1,
            )

        return self._change(execution_id, expected_revision, change)

    def put_phase_result(
        self,
        execution_id: str,
        phase: str,
        artifact: Mapping[str, object],
        *,
        expected_revision: int | None = None,
    ) -> ExecutionDocument:
        if not isinstance(phase, str) or not phase.strip():
            raise ExecutionStoreError("phase must be non-empty text")
        if not isinstance(artifact, Mapping):
            raise ExecutionStoreError("artifact must be a mapping")
        return self._change(
            execution_id,
            expected_revision,
            lambda document: replace(
                document,
                phase_results={**document.phase_results, phase.strip(): dict(artifact)},
                revision=document.revision + 1,
            ),
        )
