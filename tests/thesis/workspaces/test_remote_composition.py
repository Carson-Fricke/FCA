"""Remote catalog provisioning and local-mode selection contracts."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from azure.core.exceptions import ResourceNotFoundError

from thesis.http.application import serving_workspace_repositories
from thesis.workspaces.azure_catalog import AzureWorkspaceCatalogStore
from thesis.workspaces.local import LocalWorkspace, WorkspaceNotFoundError
from thesis.workspaces.manifest import load_workspace_manifest


def test_local_mode_ignores_remote_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THESIS_STORAGE_ACCOUNT_NAME", raising=False)
    monkeypatch.setenv("THESIS_GITEA_URL", "invalid")
    assert serving_workspace_repositories() is None


def test_azure_mode_rejects_incomplete_gitea_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THESIS_STORAGE_ACCOUNT_NAME", "thesisstorage")
    monkeypatch.setenv("THESIS_GITEA_URL", "https://git.example")
    monkeypatch.delenv("THESIS_GITEA_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Azure workspaces require"):
        serving_workspace_repositories()


def test_catalog_returns_only_after_repository_is_seeded(tmp_path: Path) -> None:
    blobs = Mock()
    container = blobs.get_container_client.return_value
    container.download_blob.side_effect = ResourceNotFoundError()
    template = load_workspace_manifest("config/thesis/workspace.yaml")
    repositories = Mock()
    repositories.open.side_effect = WorkspaceNotFoundError("not provisioned")
    repositories.repository_uri.return_value = (
        "https://git.example/workspaces/remote-demo.git"
    )
    repositories.create.side_effect = lambda manifest: LocalWorkspace(
        tmp_path, manifest
    )
    store = AzureWorkspaceCatalogStore(blobs, template, repositories=repositories)
    summary = store.add({"id": "remote-demo", "name": "Remote demo"})
    created = repositories.create.call_args.args[0]
    assert created.repository_identity.provider == "azure"
    assert summary["owner_id"] == created.owner.owner_id
    repositories.open.return_value = LocalWorkspace(tmp_path, created)
    repositories.open.side_effect = None
    assert store.get("remote-demo") == summary
    repositories.create.side_effect = RuntimeError("remote unavailable")
    repositories.open.side_effect = WorkspaceNotFoundError("not provisioned")
    with pytest.raises(RuntimeError, match="remote unavailable"):
        store.add({"id": "another-demo", "name": "Another"})
