"""Tests for the composed Thesis HTTP application boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fastapi.testclient import TestClient

from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import OperatorPolicyRegistry
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat import (
    AzureBlobChatPersistence,
    ChatService,
    InMemoryChatPersistence,
    SubAgentStatus,
    WorkflowDefinition,
    WorkflowExecutionState,
)
from thesis.chat.contracts import WORKFLOW_NODE_ROLE
from thesis.chat.persistence import FileChatPersistence
from thesis.http.application import (
    create_app,
    durable_chat_service,
    serving_workspace_store,
)
from thesis.http.workspaces import build_local_workspace_data
from thesis.providers.openrouter import OpenRouterProfile, OpenRouterProvider
from thesis.providers.transports import DEFAULT_SYSTEM_PROMPT
from thesis.workspaces.azure_catalog import AzureWorkspaceCatalogStore

TOKEN = "thesis-test-token"
WORKFLOW = WorkflowDefinition(
    workflow_id="strategy-development",
    version="1.0.0",
    title="Strategy development",
    phases=("understand", "implement"),
)


def test_health_is_public_and_auth_check_requires_token() -> None:
    client = TestClient(create_app(bearer_token=TOKEN))

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/v1/auth/check").status_code == 401
    assert client.get(
        "/v1/auth/check", headers={"Authorization": f"Bearer {TOKEN}"}
    ).json() == {"status": "ok"}
    rejected = client.get(
        "/v1/auth/check", headers={"Authorization": "Bearer wrong-token"}
    )
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "unauthorized"
    assert "WWW-Authenticate" in rejected.headers


def test_api_fails_closed_without_configured_token(monkeypatch) -> None:
    monkeypatch.delenv("THESIS_API__BEARER_TOKEN", raising=False)
    client = TestClient(create_app())

    assert (
        client.get(
            "/v1/auth/check", headers={"Authorization": "Bearer anything"}
        ).status_code
        == 401
    )


def test_rejects_blank_configured_token() -> None:
    try:
        create_app(bearer_token="   ")
    except ValueError as error:
        assert "bearer token" in str(error)
    else:
        raise AssertionError("blank bearer token must be rejected")


def test_chat_surface_composed_with_injected_service(
    client: TestClient, launch
) -> None:
    chat = client.post(
        "/v1/workspaces/workspace-a/chats",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    launch(chat["chat_id"])

    listed = client.get(
        f"/v1/workspaces/workspace-a/chats/{chat['chat_id']}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()
    assert listed["chat_id"] == chat["chat_id"]
    assert listed["links"]["workflow_execution_ids"] == ["execution-001"]

    unauthenticated = TestClient(create_app(bearer_token=TOKEN))
    assert (
        unauthenticated.get(
            f"/v1/workspaces/workspace-a/chats/{chat['chat_id']}"
        ).status_code
        == 401
    )


class _KeySource:
    def key(self) -> str:
        return "sk-test-secret"


class _RecordingTransport:
    def __init__(self, body: str) -> None:
        self._body = body
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        self.calls.append(dict(payload))
        return 200, self._body


def _completion_body(text: str) -> str:
    return json.dumps(
        {
            "model": "test/model",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )


def _model_provider(transport: _RecordingTransport) -> OpenRouterProvider:
    return OpenRouterProvider(
        OpenRouterProfile(profile_id="openrouter-test", model="test/model"),
        _KeySource(),
        transport,
    )


def _compose_reply(app, transport: _RecordingTransport) -> str:
    service = app.state.chat_service
    conversations = app.state.conversations
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    service.create_message(
        "workspace-a", chat.chat_id, actor_id="operator", body="hello"
    )
    history = service.list_messages("workspace-a", chat.chat_id)
    chunks = conversations.reply_for("workspace-a", chat.chat_id, history)
    assert "".join(chunk for chunk in chunks if isinstance(chunk, str)) == "Hi"
    return str(transport.calls[0]["messages"][0]["content"])


def test_create_app_composes_plain_reply_service_without_tools() -> None:
    transport = _RecordingTransport(_completion_body("Hi"))

    app = create_app(bearer_token=TOKEN, model_provider=_model_provider(transport))
    system = _compose_reply(app, transport)

    assert "tools" not in transport.calls[0]
    assert system == DEFAULT_SYSTEM_PROMPT


def test_create_app_exposes_the_human_only_operator_policy() -> None:
    app = create_app(bearer_token=TOKEN)

    assert isinstance(app.state.operator_policies, OperatorPolicyRegistry)


def test_create_app_exposes_the_shared_operator_policy_registry() -> None:
    policies = OperatorPolicyRegistry()

    app = create_app(bearer_token=TOKEN, operator_policies=policies)

    assert app.state.operator_policies is policies


def test_retired_validation_routes_are_absent() -> None:
    client = TestClient(create_app(bearer_token=TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}

    assert (
        client.get("/v1/workspaces/workspace-a/validation", headers=headers).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/workspaces/workspace-a/validation",
            headers=headers,
            json={"claim": "old claim"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/v1/workspaces/workspace-a/validation/val-1", headers=headers
        ).status_code
        == 404
    )


def test_durable_chat_service_uses_the_filesystem_without_a_storage_account(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("THESIS_STORAGE_ACCOUNT_NAME", raising=False)
    monkeypatch.setenv("THESIS_WORKSPACE_ROOT", str(tmp_path))

    service = durable_chat_service()
    assert isinstance(service.persistence, FileChatPersistence)

    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    message = service.create_message(
        "workspace-a", chat.chat_id, actor_id="operator", body="Hello"
    )

    restarted = durable_chat_service()
    assert restarted.get_message("workspace-a", message.message_id).body == "Hello"


def test_durable_chat_service_uses_azure_blobs_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("THESIS_STORAGE_ACCOUNT_NAME", "thesiswstest")
    monkeypatch.setenv("THESIS_WORKSPACE_CONTAINER_PREFIX", "thesis-ws-")

    service = durable_chat_service()

    assert isinstance(service.persistence, AzureBlobChatPersistence)
    assert service.persistence.container_prefix == "thesis-ws-"


def test_serving_workspace_store_is_none_without_a_storage_account(monkeypatch) -> None:
    monkeypatch.delenv("THESIS_STORAGE_ACCOUNT_NAME", raising=False)
    assert serving_workspace_store() is None


def test_serving_workspace_store_uses_azure_blobs_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("THESIS_STORAGE_ACCOUNT_NAME", "thesiswstest")
    monkeypatch.setenv("THESIS_WORKSPACE_CONTAINER_PREFIX", "thesis-ws-")

    store = serving_workspace_store()

    assert isinstance(store, AzureWorkspaceCatalogStore)
    assert store.container_prefix == "thesis-ws-"


def test_startup_reconciliation_cancels_stranded_sub_agents() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    bundle = build_local_workspace_data(chat_service=chat)
    workspace_id = str(bundle.workspaces.create_workspace("Alpha Lab")["id"])
    chat_id = chat.create_chat(
        workspace_id, title="Research", owner_actor_id="operator"
    ).chat_id
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    node = lifecycle.spawn(
        workspace_id,
        chat_id,
        spec=SubAgentSpec(role=WORKFLOW_NODE_ROLE),
        actor_id="operator",
        workflow_execution_id="execution-stranded",
    )

    create_app(bearer_token=TOKEN, chat_service=chat, workspace_data=bundle)

    stored = store.get_sub_agent(workspace_id, node.sub_agent_id)
    assert stored is not None and stored.status is SubAgentStatus.CANCELLED


def test_startup_reconciliation_stops_executions_then_cancels_their_nodes() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    bundle = build_local_workspace_data(chat_service=chat)
    workspace_id = str(bundle.workspaces.create_workspace("Beta Lab")["id"])
    chat_id = chat.create_chat(
        workspace_id, title="Research", owner_actor_id="operator"
    ).chat_id
    execution = chat.launch_workflow(
        workspace_id,
        chat_id,
        workflow=WORKFLOW,
        actor_id="operator",
        body="Build and test the strategy.",
    )
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    node = lifecycle.spawn(
        workspace_id,
        chat_id,
        spec=SubAgentSpec(role=WORKFLOW_NODE_ROLE),
        actor_id="operator",
        workflow_execution_id=execution.execution_id,
    )

    create_app(bearer_token=TOKEN, chat_service=chat, workspace_data=bundle)

    stopped = chat.get_execution(workspace_id, execution.execution_id)
    assert stopped.state is WorkflowExecutionState.STOPPED
    stored = store.get_sub_agent(workspace_id, node.sub_agent_id)
    assert stored is not None and stored.status is SubAgentStatus.CANCELLED
