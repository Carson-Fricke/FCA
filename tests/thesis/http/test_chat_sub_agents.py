"""Chat-scoped durable sub-agent projection and routes."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from thesis.agent.runtime.delivery import NodeInputService
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import (
    NodeOperatorPolicy,
    OperatorPolicyRegistry,
)
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.agent.runtime.tree import project_chat_sub_agents
from thesis.chat import ChatService, InMemoryChatPersistence, WorkflowDefinition
from thesis.chat.contracts import (
    SubAgentKind,
    SubAgentStatus,
    WorkflowExecutionState,
)
from thesis.execution.events import ExecutionEvent, ExecutionEventType
from thesis.execution.sub_agents import SubAgentContextSpec, append_sub_agent_event
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
NOW = datetime(2026, 1, 1, tzinfo=UTC)
WORKFLOW = WorkflowDefinition(
    workflow_id="strategy-development",
    version="1.0.0",
    title="Strategy development",
    phases=("understand", "implement", "evaluate"),
)


def _service() -> tuple[ChatService, str]:
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(WORKSPACE, title="Test chat", owner_actor_id=ACTOR)
    return chat, thread.chat_id


def _execution(chat: ChatService, chat_id: str) -> str:
    """Launch a durable workflow execution and return its id."""
    execution = chat.launch_workflow(
        WORKSPACE,
        chat_id,
        workflow=WORKFLOW,
        actor_id=ACTOR,
        body="Build and test the strategy.",
    )
    return execution.execution_id


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


def test_operator_answer_resumes_a_paused_node() -> None:
    chat, chat_id = _service()
    child = _spawn(chat, chat_id, kind=SubAgentKind.ONE_SHOT)
    store = PersistenceSubAgentStore(chat.persistence)
    NodeInputService(chat=chat, store=store).request_input(WORKSPACE, child)
    paused = store.get_sub_agent(WORKSPACE, child)
    assert paused is not None and paused.status is SubAgentStatus.NEEDS_INPUT

    response = _client(chat).post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{child}/messages",
        headers=_headers(),
        json={"body": "please continue"},
    )

    assert response.status_code == 200
    resumed = store.get_sub_agent(WORKSPACE, child)
    assert resumed is not None
    assert resumed.status is SubAgentStatus.RUNNING
    assert resumed.accepts_input is False


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


def _spawn_bound(
    chat: ChatService,
    chat_id: str,
    execution_id: str,
    *,
    sub_agent_id: str,
    parent_sub_agent_id: str | None = None,
    kind: SubAgentKind = SubAgentKind.CONVERSATION,
    start: bool = False,
) -> str:
    """Spawn one durable phase agent attributed to a workflow execution."""
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    record = lifecycle.spawn(
        WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role=f"Thesis {sub_agent_id} orchestrator", kind=kind),
        actor_id=ACTOR,
        parent_sub_agent_id=parent_sub_agent_id,
        workflow_execution_id=execution_id,
        sub_agent_id=sub_agent_id,
    )
    if start:
        lifecycle.start(WORKSPACE, sub_agent_id)
    return record.sub_agent_id


def _seed_event(
    chat: ChatService,
    execution_id: str,
    sub_agent_id: str,
    parent_sub_agent_id: str | None,
    *,
    event_type: ExecutionEventType = ExecutionEventType.SUB_AGENT_SPAWNED,
    accepts_input: bool = False,
    extra: dict[str, object] | None = None,
) -> ExecutionEvent:
    return append_sub_agent_event(
        chat.event_store,
        execution_id=execution_id,
        workspace_id=WORKSPACE,
        event_type=event_type,
        occurred_at=NOW,
        sub_agent_id=sub_agent_id,
        parent_sub_agent_id=parent_sub_agent_id,
        phase="test_phase",
        role="orchestrator",
        context=SubAgentContextSpec(),
        accepts_input=accepts_input,
        extra=extra,
    )


def test_node_events_own_versus_descendants_preserve_the_global_cursor() -> None:
    chat, chat_id = _service()
    execution_id = _execution(chat, chat_id)
    root = _spawn_bound(chat, chat_id, execution_id, sub_agent_id="agent-a")
    _spawn_bound(
        chat, chat_id, execution_id, sub_agent_id="agent-b", parent_sub_agent_id=root
    )
    _spawn_bound(
        chat,
        chat_id,
        execution_id,
        sub_agent_id="agent-c",
        parent_sub_agent_id="agent-b",
    )
    second = _seed_event(chat, execution_id, "agent-b", root)
    _seed_event(
        chat,
        execution_id,
        "agent-c",
        "agent-b",
        event_type=ExecutionEventType.SUB_AGENT_PROGRESS,
        extra={"kind": "tool", "toolCall": {"name": "backtest"}},
    )
    base = f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}"

    own = (
        _client(chat)
        .get(base + "/sub_agents/agent-b/events", headers=_headers())
        .json()
    )
    assert [event["payload"]["subAgentId"] for event in own["events"]] == ["agent-b"]
    assert own["events"][0]["cursor"] == second.cursor

    descendants = (
        _client(chat)
        .get(
            base + "/sub_agents/agent-b/events?include_descendants=true",
            headers=_headers(),
        )
        .json()
    )
    assert {event["payload"]["subAgentId"] for event in descendants["events"]} == {
        "agent-b",
        "agent-c",
    }
    cursors = [event["cursor"] for event in descendants["events"]]
    assert cursors == sorted(cursors)
    assert cursors[0] == second.cursor

    resumed = (
        _client(chat)
        .get(
            base
            + "/sub_agents/agent-b/events"
            + f"?include_descendants=true&after_cursor={second.cursor}",
            headers=_headers(),
        )
        .json()
    )
    assert resumed["afterCursor"] == second.cursor
    assert all(event["cursor"] > second.cursor for event in resumed["events"])

    exhausted = (
        _client(chat)
        .get(
            base
            + "/sub_agents/agent-b/events"
            + f"?include_descendants=true&after_cursor={descendants['nextCursor']}",
            headers=_headers(),
        )
        .json()
    )
    assert exhausted["events"] == []


def test_node_event_stream_emits_only_the_node_scope() -> None:
    from fastapi import FastAPI as _FastAPI

    chat, chat_id = _service()
    execution_id = _execution(chat, chat_id)
    root = _spawn_bound(chat, chat_id, execution_id, sub_agent_id="agent-a")
    _spawn_bound(
        chat, chat_id, execution_id, sub_agent_id="agent-b", parent_sub_agent_id=root
    )
    _seed_event(chat, execution_id, "agent-b", root)
    _seed_event(
        chat,
        execution_id,
        "agent-b",
        root,
        event_type=ExecutionEventType.SUB_AGENT_PROGRESS,
        extra={"kind": "thinking", "text": "weighing evidence"},
    )
    _spawn_bound(
        chat, chat_id, execution_id, sub_agent_id="agent-c", parent_sub_agent_id=root
    )

    app = _FastAPI()
    app.state.chat_service = chat
    app.include_router(
        create_chat_sub_agent_router(max_poll_cycles=1),
    )
    _with_principal(app, SESSION_PRINCIPAL)
    client = TestClient(app)

    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/agent-b/events/stream",
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.text.count("event: sub_agent.") == 2
    assert '"subAgentId":"agent-b"' in response.text
    assert '"subAgentId":"agent-c"' not in response.text


def test_node_events_require_a_workflow_execution() -> None:
    chat, chat_id = _service()
    child = _spawn(chat, chat_id)

    response = _client(chat).get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{child}/events",
        headers=_headers(),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "sub_agent_without_execution"


def test_cancel_cascades_and_stops_the_execution() -> None:
    chat, chat_id = _service()
    execution_id = _execution(chat, chat_id)
    node = _spawn_bound(chat, chat_id, execution_id, sub_agent_id="agent-root")
    child = _spawn_bound(
        chat,
        chat_id,
        execution_id,
        sub_agent_id="agent-child",
        parent_sub_agent_id=node,
    )

    response = _client(chat).post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{node}/cancel",
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["subAgentId"] == node
    assert set(body["cancelledSubAgentIds"]) == {node, child}
    assert body["stoppedExecutionId"] == execution_id
    store = PersistenceSubAgentStore(chat.persistence)
    stored_node = store.get_sub_agent(WORKSPACE, node)
    stored_child = store.get_sub_agent(WORKSPACE, child)
    assert stored_node is not None and stored_node.status is SubAgentStatus.CANCELLED
    assert stored_child is not None and stored_child.status is SubAgentStatus.CANCELLED
    assert (
        chat.get_execution(WORKSPACE, execution_id).state
        is WorkflowExecutionState.STOPPED
    )
    # The stop is attributed to the authenticated operator, never the body.
    stopped = [
        event
        for event in chat.replay_events(WORKSPACE, execution_id).events
        if event.event_type is ExecutionEventType.WORKFLOW_STOPPED
    ]
    assert stopped
    assert stopped[-1].actor_id == authenticated_actor_id(TOKEN)


def test_cancel_unknown_sub_agent_returns_not_found() -> None:
    chat, chat_id = _service()

    response = _client(chat).post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/missing/cancel",
        headers=_headers(),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "sub_agent_not_found"


def test_cancel_fails_closed_without_an_exposed_principal() -> None:
    chat, chat_id = _service()
    child = _spawn(chat, chat_id)
    app = FastAPI()
    app.state.chat_service = chat
    app.include_router(create_chat_sub_agent_router())
    client = TestClient(app)

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{child}/cancel"
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "actor_identity_unavailable"
    stored = PersistenceSubAgentStore(chat.persistence).get_sub_agent(WORKSPACE, child)
    assert stored is not None and stored.status is SubAgentStatus.PENDING


def test_cancel_is_chat_scoped_and_idempotent() -> None:
    chat, chat_id = _service()
    other_chat_id = chat.create_chat(
        WORKSPACE, title="Other chat", owner_actor_id=ACTOR
    ).chat_id
    node = _spawn(chat, chat_id)
    other = _spawn(chat, other_chat_id)
    client = _client(chat)

    wrong_chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{other_chat_id}/sub_agents/{node}/cancel",
        headers=_headers(),
    )
    assert wrong_chat.status_code == 404

    first = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{node}/cancel",
        headers=_headers(),
    )
    assert first.status_code == 200
    assert first.json()["cancelledSubAgentIds"] == [node]

    # A rerun is a no-op and reports nothing newly cancelled.
    second = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{node}/cancel",
        headers=_headers(),
    )
    assert second.status_code == 200
    assert second.json()["cancelledSubAgentIds"] == []

    store = PersistenceSubAgentStore(chat.persistence)
    stored_node = store.get_sub_agent(WORKSPACE, node)
    stored_other = store.get_sub_agent(WORKSPACE, other)
    assert stored_node is not None and stored_node.status is SubAgentStatus.CANCELLED
    assert stored_other is not None and stored_other.status is SubAgentStatus.PENDING


def test_operator_request_leaves_siblings_and_the_workflow_untouched() -> None:
    chat, chat_id = _service()
    execution_id = _execution(chat, chat_id)
    node = _spawn_bound(chat, chat_id, execution_id, sub_agent_id="agent-root")
    asking = _spawn_bound(
        chat,
        chat_id,
        execution_id,
        sub_agent_id="agent-asking",
        parent_sub_agent_id=node,
        kind=SubAgentKind.ONE_SHOT,
        start=True,
    )
    sibling = _spawn_bound(
        chat,
        chat_id,
        execution_id,
        sub_agent_id="agent-sibling",
        parent_sub_agent_id=node,
        kind=SubAgentKind.ONE_SHOT,
        start=True,
    )
    client = _client(chat)
    store = PersistenceSubAgentStore(chat.persistence)

    # A one-shot agent normally forbids interruption...
    rejected = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{asking}/messages",
        headers=_headers(),
        json={"body": "unsolicited"},
    )
    assert rejected.status_code == 409

    # ...and asking is a flag on that one node: no approval, no policy
    # configuration, and no execution state change is required.
    NodeInputService(chat=chat, store=store).request_input(WORKSPACE, asking)

    waiting = store.get_sub_agent(WORKSPACE, asking)
    assert waiting is not None
    assert waiting.status is SubAgentStatus.NEEDS_INPUT
    assert waiting.accepts_input is True
    untouched_sibling = store.get_sub_agent(WORKSPACE, sibling)
    assert untouched_sibling is not None
    assert untouched_sibling.status is SubAgentStatus.RUNNING
    assert untouched_sibling.accepts_input is False
    untouched_node = store.get_sub_agent(WORKSPACE, node)
    assert untouched_node is not None
    assert untouched_node.status is SubAgentStatus.PENDING
    assert (
        chat.get_execution(WORKSPACE, execution_id).state
        is WorkflowExecutionState.RUNNING
    )
    assert not any(
        event.event_type is ExecutionEventType.WORKFLOW_PAUSED
        for event in chat.replay_events(WORKSPACE, execution_id).events
    )

    # The reply path accepts the answer with the default human-only policy.
    accepted = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/sub_agents/{asking}/messages",
        headers=_headers(),
        json={"body": "go on"},
    )

    assert accepted.status_code == 200
    resumed = store.get_sub_agent(WORKSPACE, asking)
    assert resumed is not None
    assert resumed.status is SubAgentStatus.RUNNING
    # The sibling, the workflow node, and the execution stay exactly as they
    # were: the request never affects anything but the asking node.
    still_sibling = store.get_sub_agent(WORKSPACE, sibling)
    assert still_sibling is not None
    assert still_sibling.status is SubAgentStatus.RUNNING
    still_node = store.get_sub_agent(WORKSPACE, node)
    assert still_node is not None
    assert still_node.status is SubAgentStatus.PENDING
    assert (
        chat.get_execution(WORKSPACE, execution_id).state
        is WorkflowExecutionState.RUNNING
    )
