"""Chat-scoped durable sub-agent projection and routes."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import (
    NodeOperatorPolicy,
    OperatorPolicyRegistry,
)
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.agent.runtime.tree import project_chat_sub_agents
from thesis.chat import ChatService, InMemoryChatPersistence
from thesis.chat.contracts import SubAgentKind
from thesis.http.application import create_app
from thesis.http.chat_sub_agents import (
    AUTHENTICATED_PRINCIPAL_STATE_KEY,
    authenticated_actor_id,
    create_chat_sub_agent_router,
)

TOKEN = "thesis-test-token"
WORKSPACE = "ws-test"
ACTOR = "operator-1"
SESSION_PRINCIPAL = "session:acct-9"


def _service() -> tuple[ChatService, str]:
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(WORKSPACE, title="Test chat", owner_actor_id=ACTOR)
    return chat, thread.chat_id


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _with_principal(app: FastAPI, principal: str) -> FastAPI:
    """Expose the authenticated principal the shared auth dependency returns."""

    @app.middleware("http")
    async def _principal(request: Request, call_next):
        setattr(request.state, AUTHENTICATED_PRINCIPAL_STATE_KEY, principal)
        return await call_next(request)

    return app


def _client(chat: ChatService) -> TestClient:
    return TestClient(create_app(bearer_token=TOKEN, chat_service=chat))


def _spawn(
    chat: ChatService,
    chat_id: str,
    *,
    role: str = "research",
    kind: SubAgentKind = SubAgentKind.CONVERSATION,
    parent_sub_agent_id: str | None = None,
) -> str:
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    record = lifecycle.spawn(
        WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role=role, kind=kind, allowed_tools=("run_study",)),
        actor_id=ACTOR,
        parent_sub_agent_id=parent_sub_agent_id,
    )
    return record.sub_agent_id


def test_projection_is_pure_and_nests_children() -> None:
    chat, chat_id = _service()
    store = PersistenceSubAgentStore(chat.persistence)
    root = _spawn(chat, chat_id)
    child = _spawn(chat, chat_id, parent_sub_agent_id=root)

    records = store.list_sub_agents(WORKSPACE)
    first = project_chat_sub_agents(records, workspace_id=WORKSPACE, chat_id=chat_id)
    second = project_chat_sub_agents(records, workspace_id=WORKSPACE, chat_id=chat_id)

    assert [node.sub_agent_id for node in first.roots] == [root]
    assert [node.sub_agent_id for node in first.roots[0].children] == [child]
    assert set(first.ids()) == {root, child}
    assert first.as_dict() == second.as_dict()


def test_list_returns_the_chat_scoped_tree() -> None:
    chat, chat_id = _service()
    root = _spawn(chat, chat_id)

    response = _client(chat).get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents", headers=_headers()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["chatId"] == chat_id
    assert [node["subAgentId"] for node in body["roots"]] == [root]
    assert body["roots"][0]["status"] == "pending"


def test_get_and_children_return_one_node() -> None:
    chat, chat_id = _service()
    root = _spawn(chat, chat_id)
    child = _spawn(chat, chat_id, parent_sub_agent_id=root)
    client = _client(chat)

    node = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{root}",
        headers=_headers(),
    )
    assert node.status_code == 200
    assert node.json()["subAgentId"] == root

    children = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{root}/sub_agents",
        headers=_headers(),
    )
    assert children.status_code == 200
    assert [item["subAgentId"] for item in children.json()["items"]] == [child]


def test_unknown_sub_agent_returns_not_found() -> None:
    chat, chat_id = _service()

    response = _client(chat).get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/missing",
        headers=_headers(),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "sub_agent_not_found"


def test_operator_input_uses_the_authenticated_actor() -> None:
    chat, chat_id = _service()
    child = _spawn(chat, chat_id)

    response = _client(chat).post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{child}/messages",
        headers=_headers(),
        json={"body": "please continue"},
    )

    # The shared authenticator exposes the bearer principal it validated, and
    # the route derives a stable, non-secret actor id from it: never the body.
    expected_actor = authenticated_actor_id(TOKEN)
    assert response.status_code == 200
    assert response.json()["subAgentId"] == child
    assert response.json()["actor_id"] == expected_actor
    assert response.json()["operatorPolicy"]["actorKind"] == "human_operator"
    messages = chat.list_messages(WORKSPACE, chat_id)
    persisted = [message for message in messages if message.body == "please continue"]
    assert len(persisted) == 1
    assert persisted[0].actor_id == expected_actor
    assert persisted[0].data is not None
    assert persisted[0].data["actorKind"] == "human_operator"


def test_operator_input_rejects_a_body_supplied_actor() -> None:
    chat, chat_id = _service()
    child = _spawn(chat, chat_id)

    response = _client(chat).post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{child}/messages",
        headers=_headers(),
        json={"actor_id": ACTOR, "body": "please continue"},
    )

    assert response.status_code == 422
    assert chat.list_messages(WORKSPACE, chat_id) == ()


def test_operator_input_fails_closed_without_an_exposed_principal() -> None:
    chat, chat_id = _service()
    child = _spawn(chat, chat_id)
    # A composition with no authenticator exposing a principal: the route must
    # refuse to invent an actor rather than trusting anything else.
    app = FastAPI()
    app.state.chat_service = chat
    app.include_router(create_chat_sub_agent_router())
    client = TestClient(app)

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{child}/messages",
        json={"body": "please continue"},
    )

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["code"] == "actor_identity_unavailable"
    assert "authenticated_principal" in detail["message"]
    assert chat.list_messages(WORKSPACE, chat_id) == ()


def test_operator_policy_denial_fails_closed_with_provenance() -> None:
    chat, chat_id = _service()
    child = _spawn(chat, chat_id)
    app = FastAPI()
    app.state.chat_service = chat
    app.include_router(
        create_chat_sub_agent_router(
            operator_policies=OperatorPolicyRegistry(
                default=NodeOperatorPolicy(allowed_actor_kinds=frozenset())
            )
        )
    )
    _with_principal(app, SESSION_PRINCIPAL)
    client = TestClient(app)

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{child}/messages",
        json={"body": "please continue"},
    )

    assert response.status_code == 403
    body = response.json()["detail"]
    assert body["code"] == "operator_policy_denied"
    assert body["actorKind"] == "human_operator"
    assert body["allowedActorKinds"] == []
    assert chat.list_messages(WORKSPACE, chat_id) == ()


def test_one_shot_child_rejects_unsolicited_input() -> None:
    chat, chat_id = _service()
    child = _spawn(chat, chat_id, kind=SubAgentKind.ONE_SHOT)

    response = _client(chat).post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{child}/messages",
        headers=_headers(),
        json={"body": "hello"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "sub_agent_not_accepting_input"


def test_tree_is_reconstructed_from_durable_records_after_restart() -> None:
    chat, chat_id = _service()
    root = _spawn(chat, chat_id)
    child = _spawn(chat, chat_id, parent_sub_agent_id=root)

    fresh_client = _client(chat)
    response = fresh_client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents", headers=_headers()
    )

    body = response.json()
    assert set(node["subAgentId"] for node in body["items"]) == {root, child}
