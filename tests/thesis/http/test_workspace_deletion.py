"""Workspace deletion covers every storage scope and survives interrupted cleanup."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient
from fastapi.testclient import TestClient

from thesis.auth import (
    BrowserSecurity,
    OperatorSessionService,
    RuntimeSecurity,
    WorkspaceAuthorizer,
)
from thesis.http.application import create_app
from thesis.http.auth import CSRF_HEADER_NAME, SESSION_COOKIE_NAME, OidcVerifier
from thesis.http.workspaces import build_local_workspace_data, manifest_owner_lookup
from thesis.identity.local import LocalAccountStore, LocalSessionStore
from thesis.workspaces.azure_catalog import DELETION_PATH, AzureWorkspaceCatalogStore
from thesis.workspaces.local import LocalWorkspace, WorkspaceNotFoundError
from thesis.workspaces.manifest import WorkspaceManifest, load_workspace_manifest
from thesis_runtime.adapters.filesystem.records import FileChatPersistence
from thesis_runtime.contracts.records import ChatState, ChatThread, RecordNotFound
from thesis_runtime.conversations import ChatService, WorkflowDefinition
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.workflows.registry import WorkflowRegistry

HEADERS = {"Authorization": "Bearer test-token"}


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_delete_workspace_fences_all_routes_and_conversations(prefix: str) -> None:
    app = create_app(bearer_token="test-token", workflow_registry=WorkflowRegistry())
    client = TestClient(app)
    workspace = client.post(
        "/v1/workspaces", headers=HEADERS, json={"name": "Delete me"}
    ).json()
    other = client.post(
        "/v1/workspaces", headers=HEADERS, json={"name": "Keep me"}
    ).json()
    workspace_id = workspace["id"]
    chat = app.state.chat_service
    conversations = app.state.conversations
    root = chat.create_chat(workspace_id, title="Active", owner_actor_id="operator")
    archived = chat.create_chat(
        workspace_id, title="Archived", owner_actor_id="operator"
    )
    chat.archive_chat(workspace_id, archived.chat_id)
    retained_handle = conversations.root(workspace_id, root.chat_id)
    retained_handle.send(body="Private history", actor_id="operator")
    execution = chat.launch_workflow(
        workspace_id,
        root.chat_id,
        workflow=WorkflowDefinition(
            workflow_id="deletion-test",
            version="1.0.0",
            title="Delete",
            phases=("research",),
        ),
        actor_id="operator",
        body="Work in progress",
    )
    keep = chat.create_chat(other["id"], title="Keep", owner_actor_id="operator")
    path = f"/v1/workspaces/{workspace_id}"

    assert client.delete(prefix + path).status_code == 401
    assert client.get(path, headers=HEADERS).status_code == 200
    response = client.delete(prefix + path, headers=HEADERS)
    assert response.status_code == 202
    assert response.json() == {"id": workspace_id, "status": "deletion_pending"}
    for surface in ("", "/api"):
        assert client.get(surface + "/v1/workspaces", headers=HEADERS).json()[
            "items"
        ] == [other]
        for suffix in (
            "",
            "/snapshot",
            "/chats",
            f"/chats/{root.chat_id}/messages",
            "/state",
            "/strategies",
            "/artifacts",
            "/dashboards/widgets",
            f"/executions/{execution.execution_id}",
        ):
            assert (
                client.get(surface + path + suffix, headers=HEADERS).status_code == 404
            )
        assert (
            client.post(
                surface + path + "/chats",
                headers=HEADERS,
                json={"title": "Late", "owner_actor_id": "operator"},
            ).status_code
            == 404
        )
        assert client.delete(surface + path, headers=HEADERS).status_code == 202
    assert chat.list_chats(workspace_id) == ()
    assert chat.get_chat(other["id"], keep.chat_id) == keep
    with pytest.raises(RecordNotFound, match="workspace has been deleted"):
        chat.create_chat(workspace_id, title="Late", owner_actor_id="operator")
    with pytest.raises(RecordNotFound):
        retained_handle.send(body="Late reply", actor_id="operator")
    with pytest.raises(RecordNotFound):
        chat.get_execution(workspace_id, execution.execution_id)
    replacement = client.post(
        "/v1/workspaces", headers=HEADERS, json={"name": "Delete me"}
    ).json()
    assert replacement["id"] != workspace_id
    assert client.delete("/v1/workspaces/missing", headers=HEADERS).status_code == 404


def test_local_deletion_marker_survives_restart_and_resumes_chat_cleanup(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspaces"
    record_root = tmp_path / "runtime"
    persistence = FileChatPersistence(record_root)
    chat = ChatService(persistence)
    data = build_local_workspace_data(workspace_root=workspace_root, chat_service=chat)
    workspace = data.workspaces.create_workspace("Persisted")
    workspace_id = str(workspace["id"])
    root = chat.create_chat(workspace_id, title="History", owner_actor_id="operator")
    Conversations(chat=chat).root(workspace_id, root.chat_id).send(
        body="Private", actor_id="operator"
    )
    (workspace_root / workspace_id / "private.txt").write_text(
        "Pending deletion", encoding="utf-8"
    )
    # Simulate interruption immediately after the durable workspace marker,
    # before any of its conversations have been individually marked.
    data.workspaces.store.mark_deleted(workspace_id, "2026-10-12T00:00:00Z")
    original = persistence.get(ChatThread, workspace_id, root.chat_id)
    assert original is not None and original.state is ChatState.ACTIVE

    reloaded_persistence = FileChatPersistence(record_root)
    reloaded_chat = ChatService(reloaded_persistence)
    reloaded_data = build_local_workspace_data(
        workspace_root=workspace_root, chat_service=reloaded_chat
    )
    app = create_app(
        bearer_token="test-token",
        chat_service=reloaded_chat,
        workspace_data=reloaded_data,
        workflow_registry=WorkflowRegistry(),
    )
    with TestClient(app) as client:
        assert client.get("/v1/workspaces", headers=HEADERS).json() == {"items": []}
        assert (
            client.get(
                f"/v1/workspaces/{workspace_id}/state", headers=HEADERS
            ).status_code
            == 404
        )
        tombstone = reloaded_persistence.get(ChatThread, workspace_id, root.chat_id)
        assert tombstone is not None and tombstone.state is ChatState.DELETED
        assert (
            client.delete(
                f"/api/v1/workspaces/{workspace_id}", headers=HEADERS
            ).status_code
            == 202
        )
        assert reloaded_data.workspaces.store.deleted_workspace_ids() == (workspace_id,)
        with pytest.raises(RecordNotFound):
            reloaded_chat.create_chat(
                workspace_id, title="Resurrect", owner_actor_id="operator"
            )
    # The parent marker covers repository files and history too; physical
    # removal of non-conversation storage is deliberately deferred.
    assert (workspace_root / workspace_id / "private.txt").exists()


def test_hosted_deletion_covers_remote_repository_and_recovers_unknown_outcome(
    tmp_path: Path,
) -> None:
    """Fake only Blob/Gitea transport; exercise real HTTP/catalog/runtime services."""
    blobs = Mock(spec=BlobServiceClient)
    contents: dict[str, dict[str, object]] = {}
    containers: dict[str, Mock] = {}
    interrupt = True

    def container_for(name: str) -> Mock:
        if name in containers:
            return containers[name]
        client = Mock(spec=ContainerClient)
        containers[name] = client
        contents[name] = {}

        def download(path: str) -> SimpleNamespace:
            if path not in contents[name]:
                raise ResourceNotFoundError()
            return SimpleNamespace(readall=lambda: str(contents[name][path]).encode())

        def upload(path: str, value: object, *, overwrite: bool = False) -> None:
            nonlocal interrupt
            if path in contents[name] and not overwrite:
                raise ResourceExistsError()
            contents[name][path] = value
            if path == DELETION_PATH and interrupt:
                interrupt = False
                raise RuntimeError("connection interrupted after marker commit")

        client.download_blob.side_effect = download
        client.upload_blob.side_effect = upload
        return client

    blobs.get_container_client.side_effect = container_for
    blobs.list_containers.side_effect = lambda **_: [
        SimpleNamespace(name=name) for name in containers
    ]
    repositories = Mock()
    remote: dict[str, LocalWorkspace] = {}

    def open_remote(workspace_id: str) -> LocalWorkspace:
        if workspace_id not in remote:
            raise WorkspaceNotFoundError(workspace_id)
        return remote[workspace_id]

    def create_remote(manifest: WorkspaceManifest) -> LocalWorkspace:
        remote[manifest.workspace_id] = LocalWorkspace(tmp_path, manifest)
        return remote[manifest.workspace_id]

    repositories.open.side_effect = open_remote
    repositories.create.side_effect = create_remote
    repositories.repository_uri.return_value = (
        "https://git.example/workspaces/hosted.git"
    )
    template = load_workspace_manifest("config/thesis/workspace.yaml")
    store = AzureWorkspaceCatalogStore(blobs, template, repositories=repositories)
    chat = ChatService()
    data = build_local_workspace_data(workspace_store=store, chat_service=chat)
    app = create_app(
        bearer_token="test-token",
        chat_service=chat,
        workspace_data=data,
        workflow_registry=WorkflowRegistry(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    workspace = client.post(
        "/v1/workspaces", headers=HEADERS, json={"name": "Hosted"}
    ).json()
    root = chat.create_chat(workspace["id"], title="Active", owner_actor_id="operator")
    path = f"/v1/workspaces/{workspace['id']}"
    assert client.delete(path, headers=HEADERS).status_code == 500
    assert client.get(path + "/chats", headers=HEADERS).status_code == 404
    assert chat.list_chats(workspace["id"]) == ()
    assert client.delete(path, headers=HEADERS).status_code == 202
    with pytest.raises(RecordNotFound):
        chat.get_chat(workspace["id"], root.chat_id)

    reloaded = AzureWorkspaceCatalogStore(blobs, template, repositories=repositories)
    repositories.open.side_effect = RuntimeError("deleted repository must never reopen")
    assert reloaded.list() == ()
    assert reloaded.get(workspace["id"]) is None
    assert reloaded.deleted_workspace_ids() == (workspace["id"],)
    # Keep the marker and remote mapping until the whole storage scope is
    # physically removed. Removing just the manifest would expose a synthetic row.
    assert contents["thesis-ws-hosted"][DELETION_PATH]
    with pytest.raises(ValueError, match="deleted"):
        reloaded.add(workspace)


def test_deletion_requires_owner_and_csrf_and_retains_authorized_retry(
    tmp_path: Path,
) -> None:
    chat = ChatService()
    data = build_local_workspace_data(chat_service=chat)
    workspace = data.workspaces.create_workspace("Owner workspace")
    other = data.workspaces.create_workspace("Other workspace")
    data.workspaces.store.add({**workspace, "owner_id": "owner"})
    data.workspaces.store.add({**other, "owner_id": "other"})
    sessions = LocalSessionStore(tmp_path / "sessions.json")
    session_service = OperatorSessionService(
        WorkspaceAuthorizer(manifest_owner_lookup(data.workspaces.store)),
        RuntimeSecurity(browser=BrowserSecurity(secure_cookies=True)),
        session_store=sessions,
    )
    owner = session_service.open_session("owner", str(workspace["id"]))
    attacker = session_service.open_session("other", str(other["id"]))
    app = create_app(
        chat_service=chat,
        workspace_data=data,
        account_store=LocalAccountStore(tmp_path / "accounts.json"),
        auth_verifier=Mock(spec=OidcVerifier),
        session_service=session_service,
        session_store=sessions,
        session_workspace_id=str(workspace["id"]),
        workflow_registry=WorkflowRegistry(),
    )
    client = TestClient(app, base_url="https://testserver")
    path = f"/v1/workspaces/{workspace['id']}"
    client.cookies.set(SESSION_COOKIE_NAME, attacker.session_id)
    assert (
        client.delete(path, headers={CSRF_HEADER_NAME: attacker.csrf_token}).status_code
        == 403
    )
    assert not data.workspaces.store.is_deleted(str(workspace["id"]))
    client.cookies.set(SESSION_COOKIE_NAME, owner.session_id)
    assert client.delete(path).status_code == 403
    assert (
        client.delete(path, headers={CSRF_HEADER_NAME: owner.csrf_token}).status_code
        == 202
    )
    assert (
        client.delete(
            "/api" + path, headers={CSRF_HEADER_NAME: owner.csrf_token}
        ).status_code
        == 202
    )
    client.cookies.set(SESSION_COOKIE_NAME, attacker.session_id)
    assert (
        client.delete(path, headers={CSRF_HEADER_NAME: attacker.csrf_token}).status_code
        == 403
    )
