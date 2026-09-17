"""Offline tests for the Azure Blob workspace catalog.

Every test injects an in-memory fake ``BlobServiceClient`` that mimics the
SDK's container/blob semantics, so nothing here talks to Azure.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any, cast

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

from thesis.workspaces.azure_catalog import (
    ACTIVITY_PATH,
    MANIFEST_PATH,
    AzureWorkspaceCatalogStore,
    azure_manifest_for_summary,
    workspace_container_name,
)
from thesis.workspaces.manifest import (
    dump_workspace_manifest,
    load_workspace_manifest,
)


def _template_path() -> str:
    from pathlib import Path

    return str(
        Path(__file__).resolve().parents[3] / "config" / "thesis" / "workspace.yaml"
    )


class _Download:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def readall(self) -> bytes:
        return self._content


class FakeContainer:
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

    def get_container_properties(self, **kwargs: Any) -> object:
        del kwargs
        if not self.created:
            raise ResourceNotFoundError("missing")
        return object()


class FakeBlobService:
    def __init__(self) -> None:
        self.containers: dict[str, FakeContainer] = {}

    def get_container_client(self, name: str) -> FakeContainer:
        return self.containers.setdefault(name, FakeContainer(name))

    def list_containers(
        self, name_starts_with: str = "", **kwargs: Any
    ) -> Iterable[Any]:
        del kwargs
        return [
            SimpleNamespace(name=name)
            for name in sorted(self.containers)
            if name.startswith(name_starts_with)
        ]


def _store(service: FakeBlobService) -> AzureWorkspaceCatalogStore:
    template = load_workspace_manifest(_template_path())
    return AzureWorkspaceCatalogStore(
        cast(BlobServiceClient, service), template, container_prefix="thesis-ws-"
    )


def test_workspace_container_name_matches_the_provisioned_layout() -> None:
    assert workspace_container_name("equity-momentum") == "thesis-ws-equity-momentum"
    assert workspace_container_name("Alpha_Lab", "thesis-ws-") == "thesis-ws-alpha-lab"


def test_azure_manifest_for_summary_sets_the_azure_provider() -> None:
    template = load_workspace_manifest(_template_path())
    manifest = azure_manifest_for_summary(
        template,
        {
            "id": "gamma-lab",
            "name": "Gamma Lab",
            "description": "A test workspace",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )

    assert manifest.workspace_id == "gamma-lab"
    assert manifest.repository_identity.provider == "azure"
    assert manifest.runtime_profile.mode == "azure"
    assert manifest.charter.title == "Gamma Lab"
    assert manifest.charter.objective == "A test workspace"


def test_add_provisions_a_container_and_manifest_that_list_and_get_read_back() -> None:
    service = FakeBlobService()
    store = _store(service)

    summary = store.add(
        {
            "id": "gamma-lab",
            "name": "Gamma Lab",
            "description": "A test workspace",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )

    assert summary["id"] == "gamma-lab"
    assert summary["name"] == "Gamma Lab"
    container = service.containers["thesis-ws-gamma-lab"]
    assert container.created is True
    assert MANIFEST_PATH in container.blobs
    assert store.get("gamma-lab") == summary
    assert [item["id"] for item in store.list()] == ["gamma-lab"]


def test_a_container_without_a_manifest_is_still_listed() -> None:
    service = FakeBlobService()
    store = _store(service)
    service.get_container_client("thesis-ws-equity-momentum").create_container()

    summaries = store.list()

    assert [item["id"] for item in summaries] == ["equity-momentum"]
    assert store.get("equity-momentum") is not None
    assert store.get("missing-workspace") is None


def test_activity_round_trips_inside_the_workspace_container() -> None:
    service = FakeBlobService()
    store = _store(service)
    store.add(
        {
            "id": "gamma-lab",
            "name": "Gamma Lab",
            "description": "",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )

    store.add_activity(
        {
            "id": "act-1",
            "workspace_id": "gamma-lab",
            "title": "Launched",
            "description": "A backtest launched.",
            "type": "backtest",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )

    activity = store.activity("gamma-lab")
    assert [item["id"] for item in activity] == ["act-1"]
    assert ACTIVITY_PATH in service.containers["thesis-ws-gamma-lab"].blobs


def test_dump_round_trips_through_parse() -> None:
    template = load_workspace_manifest(_template_path())
    manifest = azure_manifest_for_summary(
        template,
        {"id": "gamma-lab", "name": "Gamma Lab", "created_at": "2026-01-01T00:00:00Z"},
    )
    from thesis.workspaces.manifest import parse_workspace_manifest

    assert parse_workspace_manifest(dump_workspace_manifest(manifest)) == manifest
