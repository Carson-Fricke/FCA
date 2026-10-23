"""Indexed, bounded runnable references; executable input is read after claiming."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from thesis_runtime.execution.worker import Work, WorkReference

from .notifications import PostgresNotifications
from .store import PostgresStore, identity


class PostgresWorkStore:
    def __init__(
        self, store: PostgresStore, notifications: PostgresNotifications
    ) -> None:
        self.store = store
        self.notifications = notifications

    def enqueue(self, work: Work) -> None:
        with self.store.transaction(work.workspace_id) as connection:
            item_id = identity("RuntimeWork:", work.key)
            previous = self.store.read(
                work.workspace_id, item_id, connection=connection
            )
            if previous is not None:
                existing = self._decode(previous)
                if (existing.kind, existing.resource, existing.payload) != (
                    work.kind,
                    work.resource,
                    work.payload,
                ):
                    raise ValueError(
                        "durable work identity was reused for different inputs"
                    )
            else:
                self.store.put(
                    connection,
                    {
                        "id": item_id,
                        "workspaceId": work.workspace_id,
                        "recordType": "RuntimeWork",
                        "key": work.key,
                        "resource": work.resource,
                        "workKind": work.kind,
                        "state": "pending",
                        "payload": dict(work.payload),
                        "createdAt": datetime.now(UTC).isoformat(),
                    },
                    create=True,
                )
        self.notify()

    def notify(self) -> None:
        self.notifications.work_changed.set()

    def _references(self, limit: int = 64) -> tuple[WorkReference, ...]:
        # Active leases are excluded so a busy or long-lived resource cannot
        # occupy the first page forever and starve newly admitted work.
        rows = self.store.query(
            "record_type='RuntimeWork' AND state IN ('pending','running') "
            "AND NOT EXISTS (SELECT 1 FROM thesis_documents owner "
            "WHERE owner.collection=thesis_documents.collection "
            "AND owner.workspace_id=thesis_documents.workspace_id "
            "AND owner.record_type='Ownership' AND owner.resource=thesis_documents.resource "
            "AND (owner.body->>'expiresAt')::double precision > extract(epoch FROM clock_timestamp()))",
            columns="workspace_id,body->>'key',resource,created_at",
            order="created_at,workspace_id,id",
            limit=limit,
        )
        return tuple(WorkReference(*row) for row in rows)

    def start(self) -> tuple[WorkReference, ...]:
        return self._references()

    def wait(self, timeout: float) -> tuple[WorkReference, ...]:
        self.notifications.work_changed.wait(timeout)
        self.notifications.work_changed.clear()
        return self._references()

    def ready(self, limit: int | None = 64) -> tuple[WorkReference, ...]:
        return self._references(64 if limit is None else limit)

    @staticmethod
    def _decode(document: Mapping[str, Any]) -> Work:
        return Work(
            document["workspaceId"],
            document["key"],
            document["resource"],
            document["workKind"],
            document["payload"],
            document["state"],
            document["createdAt"],
        )

    def get(self, workspace_id: str, key: str) -> Work:
        document = self.store.read(workspace_id, identity("RuntimeWork:", key))
        if document is None:
            raise KeyError(key)
        return self._decode(document)

    def active(self, workspace_id: str, resource: str) -> bool:
        return bool(
            self.store.query(
                "workspace_id=%s AND record_type='RuntimeWork' AND resource=%s "
                "AND state IN ('pending','running')",
                [workspace_id, resource],
                columns="id",
                limit=1,
            )
        )

    def transition(self, work: Work, state: str) -> None:
        with self.store.transaction(work.workspace_id) as connection:
            document = self.store.read(
                work.workspace_id,
                identity("RuntimeWork:", work.key),
                connection=connection,
            )
            if document is None:
                raise KeyError(work.key)
            document["state"] = state
            self.store.put(connection, document)
