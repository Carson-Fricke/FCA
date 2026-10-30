"""Cosmos adapter for immutable job submission references in the catalog."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from azure.core.exceptions import HttpResponseError
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosResourceNotFoundError,
)

from thesis.persistence.submissions import require_same_submission, submission_document
from thesis_runtime.adapters.cosmos.chunks import ChunkedPayloadStore


class CosmosSubmissionStore:
    """Retain exact inputs before dispatch, independently of workflow ownership."""

    def __init__(self, container: ContainerProxy) -> None:
        self._container = container
        self._chunks = ChunkedPayloadStore(container)

    def retain(
        self,
        workspace_id: str,
        job_id: str,
        operation: str,
        inputs: Mapping[str, object],
    ) -> None:
        payload = submission_document(workspace_id, job_id, operation, inputs)
        document = {
            "id": f"JobSubmission:{job_id}",
            "workspaceId": workspace_id,
            "recordType": "JobSubmission",
            "payload": self._chunks.dump(payload, workspace_id),
        }
        try:
            self._container.create_item(document)
        except HttpResponseError as error:
            if error.status_code != 409:
                raise
            previous = self.get(workspace_id, job_id)
            if previous is None:
                raise
            require_same_submission(previous, payload)

    def _payload(
        self, document: Mapping[str, Any], workspace_id: str
    ) -> dict[str, object]:
        return self._chunks.load(document["payload"], workspace_id)

    def get(self, workspace_id: str, job_id: str) -> dict[str, object] | None:
        try:
            document = self._container.read_item(
                item=f"JobSubmission:{job_id}", partition_key=workspace_id
            )
        except CosmosResourceNotFoundError:
            return None
        return self._payload(document, workspace_id)

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        documents = self._container.query_items(
            query="SELECT c.payload FROM c WHERE c.workspaceId = @ws AND c.recordType = 'JobSubmission' ORDER BY c._ts DESC",
            parameters=[{"name": "@ws", "value": workspace_id}],
            partition_key=workspace_id,
        )
        return tuple(self._payload(document, workspace_id) for document in documents)

    def find(self, job_id: str) -> dict[str, object] | None:
        documents = list(
            self._container.query_items(
                query="SELECT * FROM c WHERE c.id = @id AND c.recordType = 'JobSubmission'",
                parameters=[{"name": "@id", "value": f"JobSubmission:{job_id}"}],
                enable_cross_partition_query=True,
            )
        )
        if len(documents) > 1:
            raise ValueError("ambiguous retained execution identity")
        return (
            None
            if not documents
            else self._payload(documents[0], documents[0]["workspaceId"])
        )


def open_submission_store() -> CosmosSubmissionStore:
    """Open the FCA-owned submission container separately from runtime state."""
    from azure.cosmos import PartitionKey

    from thesis_runtime.adapters.cosmos.client import CosmosSettings, open_cosmos_client

    settings = CosmosSettings.from_environment()
    database = open_cosmos_client(settings).get_database_client(settings.database)
    container = database.create_container_if_not_exists(
        id="submissions", partition_key=PartitionKey(path="/workspaceId")
    )
    return CosmosSubmissionStore(container)
