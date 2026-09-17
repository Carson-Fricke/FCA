"""Offline tests for the Azure Blob chat persistence adapter.

Every test injects an in-memory fake ``BlobServiceClient`` that mimics the
SDK's container/blob semantics, so nothing here talks to Azure.  The record
format is shared with the filesystem store, so these tests assert the same
round-trip contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

from thesis.chat import (
    AzureBlobChatPersistence,
    ChatService,
    MessageRole,
    TranscriptItemKind,
    WorkflowDefinition,
)
from thesis.chat.azure import workspace_container_name

NOW = datetime(2026, 1, 1, tzinfo=UTC)
WORKFLOW = WorkflowDefinition(
    workflow_id="hypothesis-development",
    version="1.0.0",
    title="Hypothesis development",
    phases=("requested", "verdict"),
)


class _Download:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def readall(self) -> bytes:
        return self._content


class _Blob:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeContainer:
    """Minimal in-memory stand-in for ``azure.storage.blob.ContainerClient``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.blobs: dict[str, bytes] = {}
        self.created = False

    def create_container(self, **kwargs: Any) -> None:
        del kwargs
        if self.created:
            raise ResourceExistsError("container already exists")
        self.created = True

    def upload_blob(
        self, name: str, data: str | bytes, *, overwrite: bool = False, **kwargs: Any
    ) -> None:
        del kwargs
        if name in self.blobs and not overwrite:
            raise ResourceExistsError("blob already exists")
        self.blobs[name] = (
            data.encode("utf-8") if isinstance(data, str) else bytes(data)
        )

    def download_blob(self, name: str, **kwargs: Any) -> _Download:
        del kwargs
        if name not in self.blobs:
            raise ResourceNotFoundError("missing")
        return _Download(self.blobs[name])

    def list_blobs(self, name_starts_with: str = "", **kwargs: Any) -> Iterable[_Blob]:
        del kwargs
        return [
            _Blob(name)
            for name in sorted(self.blobs)
            if name.startswith(name_starts_with)
        ]

    def delete_blob(self, name: str, **kwargs: Any) -> None:
        del kwargs
        if name not in self.blobs:
            raise ResourceNotFoundError("missing")
        del self.blobs[name]


class FakeBlobService:
    """Minimal in-memory stand-in for ``azure.storage.blob.BlobServiceClient``."""

    def __init__(self) -> None:
        self.containers: dict[str, FakeContainer] = {}

    def get_container_client(self, name: str) -> FakeContainer:
        return self.containers.setdefault(name, FakeContainer(name))


def _store(
    service: FakeBlobService, prefix: str = "thesis-ws-"
) -> AzureBlobChatPersistence:
    return AzureBlobChatPersistence(
        cast(BlobServiceClient, service), container_prefix=prefix
    )


def _service(service: FakeBlobService) -> ChatService:
    counters: dict[str, int] = {}

    def identifier(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]}"

    return ChatService(
        persistence=_store(service), clock=lambda: NOW, id_factory=identifier
    )


def test_workspace_container_name_matches_the_provisioned_layout() -> None:
    assert (
        workspace_container_name("equity-momentum", "thesis-ws-")
        == "thesis-ws-equity-momentum"
    )
    assert workspace_container_name("Alpha_Lab", "thesis-ws-") == "thesis-ws-alpha-lab"


def test_records_survive_a_new_service_over_the_same_account() -> None:
    service = FakeBlobService()
    first = _service(service)
    chat = first.create_chat("workspace-a", title="Research", owner_actor_id="operator")
    message = first.create_message(
        "workspace-a",
        chat.chat_id,
        actor_id="thesis-agent",
        body="The signal survives costs.",
        role=MessageRole.ASSISTANT,
        reasoning="weigh fee drag first",
    )
    anchor = first.create_message(
        "workspace-a",
        chat.chat_id,
        actor_id="operator",
        body="Hypothesis development",
        role=MessageRole.SYSTEM,
        kind=TranscriptItemKind.WORKFLOW,
        data={"executionId": "execution-1"},
    )
    execution = first.launch_workflow(
        "workspace-a", chat.chat_id, workflow=WORKFLOW, actor_id="operator", body="Go."
    )

    restarted = _service(service)
    assert restarted.get_chat("workspace-a", chat.chat_id).title == "Research"
    assert (
        restarted.get_message("workspace-a", message.message_id).reasoning
        == "weigh fee drag first"
    )
    stored_anchor = restarted.get_message("workspace-a", anchor.message_id)
    assert stored_anchor.kind is TranscriptItemKind.WORKFLOW
    assert stored_anchor.data == {"executionId": "execution-1"}
    assert (
        restarted.get_execution("workspace-a", execution.execution_id).workflow_id
        == "hypothesis-development"
    )
    assert [
        item.message_id for item in restarted.list_messages("workspace-a", chat.chat_id)
    ][:2] == [message.message_id, anchor.message_id]


def test_records_are_isolated_per_workspace_container() -> None:
    service = FakeBlobService()
    store = _store(service)
    alpha = _service(service)
    chat = alpha.create_chat("alpha-lab", title="Alpha", owner_actor_id="operator")
    alpha.create_message("alpha-lab", chat.chat_id, actor_id="operator", body="Hello")

    assert store.get(type(chat), "alpha-lab", chat.chat_id) is not None
    assert store.get(type(chat), "beta-quant", chat.chat_id) is None
    assert "thesis-ws-alpha-lab" in service.containers
    assert store.list(type(chat), "beta-quant") == ()


def test_delete_removes_the_blob() -> None:
    service = FakeBlobService()
    store = _store(service)
    chat = _service(service).create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    assert store.get(type(chat), "workspace-a", chat.chat_id) is not None

    store.delete(type(chat), "workspace-a", chat.chat_id)
    assert store.get(type(chat), "workspace-a", chat.chat_id) is None
    # Deleting a missing record is a no-op, matching the filesystem store.
    store.delete(type(chat), "workspace-a", chat.chat_id)


def test_unsupported_record_types_are_rejected() -> None:
    store = _store(FakeBlobService())
    with pytest.raises(TypeError):
        store.save(cast(Any, object()))
