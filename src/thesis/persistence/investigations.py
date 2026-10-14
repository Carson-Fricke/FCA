"""FCA-owned Cosmos persistence for research investigations."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from azure.cosmos import PartitionKey
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from thesis.research.investigations import (
    Investigation,
    Investigations,
    InvestigationState,
)
from thesis_runtime.adapters.cosmos.client import ensure_database, open_cosmos_client
from thesis_runtime.contracts.records import RecordLinks
from thesis_runtime.conversations.service import ChatService


class CosmosInvestigationStore:
    """Business records are independent of the runtime's conversation catalog."""

    def __init__(self, container: Any) -> None:
        self._container = container

    def save(self, record: Investigation) -> Investigation:
        self._container.upsert_item(
            {
                "id": self._key(record.investigation_id),
                "workspaceId": record.workspace_id,
                "record": record.as_dict(),
            }
        )
        return record

    def get(self, workspace_id: str, investigation_id: str) -> Investigation | None:
        try:
            document = self._container.read_item(
                self._key(investigation_id), workspace_id
            )
        except CosmosResourceNotFoundError:
            return None
        return self._decode(document["record"])

    def list(self, workspace_id: str) -> tuple[Investigation, ...]:
        return tuple(
            self._decode(item["record"])
            for item in self._container.query_items(
                "SELECT * FROM c WHERE c.workspaceId = @workspace",
                parameters=[{"name": "@workspace", "value": workspace_id}],
                partition_key=workspace_id,
            )
        )

    @staticmethod
    def _key(investigation_id: str) -> str:
        return hashlib.sha256(investigation_id.encode()).hexdigest()

    @staticmethod
    def _decode(data: dict[str, Any]) -> Investigation:
        links = data["links"]
        return Investigation(
            workspace_id=data["workspace_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            created_by=data["created_by"],
            investigation_id=data["investigation_id"],
            title=data["title"],
            objective=data["objective"],
            owner_actor_id=data["owner_actor_id"],
            state=InvestigationState(data["state"]),
            links=RecordLinks(
                chat_ids=tuple(links["chat_ids"]),
                workflow_execution_ids=tuple(links["workflow_execution_ids"]),
                job_ids=tuple(links["job_ids"]),
                references={
                    key: tuple(ids) for key, ids in links["references"].items()
                },
            ),
            updated_at=datetime.fromisoformat(data["updated_at"])
            if data["updated_at"]
            else None,
        )


def open_investigations(chat: ChatService) -> Investigations:
    """Compose application research storage separately from runtime persistence."""
    database = ensure_database(open_cosmos_client())
    container = database.create_container_if_not_exists(
        id="investigations", partition_key=PartitionKey(path="/workspaceId")
    )
    return Investigations(chat, CosmosInvestigationStore(container))
