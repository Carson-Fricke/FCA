"""Durable runnable references in the same workspace partition as their evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from threading import Event
from typing import Any, cast

from azure.core.exceptions import HttpResponseError
from azure.core.paging import PageIterator
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from thesis_runtime.adapters.cosmos.chunks import ChunkedPayloadStore
from thesis_runtime.adapters.cosmos.ownership import fenced_batch
from thesis_runtime.execution.worker import Work, WorkReference


def work_id(key: str) -> str:
    return "RuntimeWork:" + hashlib.sha256(key.encode()).hexdigest()


class CosmosWorkStore:
    """At-most-once starts; expired running attempts require reconciliation."""

    def __init__(self, container: ContainerProxy) -> None:
        self.container = container
        self._chunks = ChunkedPayloadStore(container)
        self._changed = Event()
        self._continuation: str | None = None
        self._page_has_changes = True

    def enqueue(self, work: Work) -> None:
        document = {
            "id": work_id(work.key),
            "workspaceId": work.workspace_id,
            "recordType": "RuntimeWork",
            "key": work.key,
            "resource": work.resource,
            "workKind": work.kind,
            "state": "pending",
            "createdAt": datetime.now(UTC).isoformat(),
            "payload": self._chunks.dump(dict(work.payload), work.workspace_id),
        }
        try:
            fenced_batch(self.container, work.workspace_id, [("create", (document,))])
        except HttpResponseError as error:
            if error.status_code != 409:
                raise
            existing = self.get(work.workspace_id, work.key)
            if (existing.kind, existing.resource, existing.payload) != (
                work.kind,
                work.resource,
                work.payload,
            ):
                raise ValueError(
                    "durable work identity was reused for different inputs"
                ) from None

        self.notify()

    def notify(self) -> None:
        """Wake this process after commit; the change feed remains authoritative."""
        self._changed.set()

    def _changes(self) -> tuple[WorkReference, ...]:
        options: dict[str, Any] = {"max_item_count": 100}
        if self._continuation is None:
            options["start_time"] = "Now"
        else:
            options["continuation"] = self._continuation
        response_headers: Mapping[str, str] = {}

        def capture(headers: Mapping[str, str], body: object) -> None:
            nonlocal response_headers
            response_headers = headers

        pages = cast(
            PageIterator,
            self.container.query_items_change_feed(
                **options, response_hook=capture
            ).by_page(),
        )
        page = list(next(pages, ()))
        self._page_has_changes = bool(page)
        # Notifications carry references only. Payload chunks are read by the
        # claimed attempt, never by every replica observing the feed.
        work = tuple(
            WorkReference(
                document["workspaceId"],
                document["key"],
                document["resource"],
                document["createdAt"],
            )
            for document in page
            if document.get("recordType") == "RuntimeWork"
            and document.get("state") in {"pending", "running"}
        )
        # The SDK raises StopIteration for a 304 before publishing the page
        # cursor. Its per-request response metadata still carries the composite
        # token (including every physical range). Keep that token through idle
        # reads; never rewind to a timestamp or depend on a host clock.
        cursor = pages.continuation_token or response_headers.get("etag")
        if not isinstance(cursor, str) or not cursor:
            raise RuntimeError("change feed response omitted its continuation token")
        self._continuation = cursor
        return work

    def start(self) -> tuple[WorkReference, ...]:
        """Capture server feed progress before snapshotting all retained work."""
        self._changes()
        documents = self.container.query_items(
            query="SELECT c.workspaceId, c.key, c.resource, c.createdAt FROM c WHERE c.recordType = 'RuntimeWork' AND c.state IN ('pending', 'running') ORDER BY c.createdAt ASC",
            parameters=[],
            enable_cross_partition_query=True,
        )
        return tuple(
            WorkReference(
                row["workspaceId"], row["key"], row["resource"], row["createdAt"]
            )
            for row in documents
        )

    def wait(self, timeout: float) -> tuple[WorkReference, ...]:
        """Consume a bounded change-feed page, independent of API process wakes."""
        if not self._page_has_changes:
            self._changed.wait(timeout)
        self._changed.clear()
        return self._changes()

    def _decode(self, document: Mapping[str, Any]) -> Work:
        workspace_id = document["workspaceId"]
        return Work(
            workspace_id,
            document["key"],
            document["resource"],
            document["workKind"],
            self._chunks.load(document["payload"], workspace_id),
            document["state"],
            document["createdAt"],
        )

    def get(self, workspace_id: str, key: str) -> Work:
        try:
            return self._decode(
                self.container.read_item(item=work_id(key), partition_key=workspace_id)
            )
        except CosmosResourceNotFoundError as error:
            raise KeyError(key) from error

    def ready(self, limit: int | None = 64) -> tuple[Work, ...]:
        documents = self.container.query_items(
            query=("SELECT TOP @limit * " if limit is not None else "SELECT * ")
            + "FROM c WHERE c.recordType = 'RuntimeWork' AND c.state IN ('pending', 'running') ORDER BY c.createdAt ASC",
            parameters=[{"name": "@limit", "value": limit}]
            if limit is not None
            else [],
            enable_cross_partition_query=True,
        )
        return tuple(self._decode(document) for document in documents)

    def active(self, workspace_id: str, resource: str) -> bool:
        return bool(
            list(
                self.container.query_items(
                    query="SELECT TOP 1 c.id FROM c WHERE c.recordType = 'RuntimeWork' AND c.resource = @resource AND c.state IN ('pending', 'running')",
                    parameters=[{"name": "@resource", "value": resource}],
                    partition_key=workspace_id,
                )
            )
        )

    def transition(self, work: Work, state: str) -> None:
        document = dict(
            self.container.read_item(
                item=work_id(work.key), partition_key=work.workspace_id
            )
        )
        document["state"] = state
        fenced_batch(
            self.container,
            work.workspace_id,
            [
                (
                    "replace",
                    (document["id"], document),
                    {"if_match_etag": document["_etag"]},
                )
            ],
        )
