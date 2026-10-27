"""HTTP tests for chat-owned messaging and percolated steering."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from thesis.http.application import create_app
from thesis.http.chat_sub_agents import authenticated_actor_id
from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    Role,
)
from thesis_runtime.adapters.providers.session_adapter import NativeSessionAdapter
from thesis_runtime.contracts.messages import from_record
from thesis_runtime.conversations import (
    ChatMessage,
    ChatService,
    MessageRole,
)
from thesis_runtime.conversations.conversation import Conversations

WORKSPACE = "workspace-a"


@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize("steering", [False, True])
def test_root_intake_uses_one_canonical_writer_and_trusted_actor(
    prefix: str,
    steering: bool,
    service: ChatService,
    launch: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = service.create_chat(
        WORKSPACE, title="Canonical", owner_actor_id="operator"
    )
    execution = launch(thread.chat_id)
    seed = service.send_message(
        WORKSPACE, thread.chat_id, actor_id="operator", body="Attachment seed"
    )
    attachment = service.create_attachment(
        WORKSPACE,
        thread.chat_id,
        seed.message_id,
        actor_id="operator",
        filename="evidence.txt",
        media_type="text/plain",
        size_bytes=0,
    )
    conversations = Conversations(chat=service)
    accepted: list[ChatMessage] = []
    scheduled: list[ChatMessage] = []
    writer = conversations.deliver_session_message

    def deliver(*args: Any, **kwargs: Any) -> ChatMessage:
        canonical = writer(*args, **kwargs)
        accepted.append(canonical)
        return canonical

    monkeypatch.setattr(conversations, "deliver_session_message", deliver)
    monkeypatch.setattr(conversations, "schedule_input", scheduled.append)
    client = TestClient(create_app(bearer_token="token", conversations=conversations))
    payload: dict[str, Any] = {
        "actor_id": "spoofed-actor",
        "body": "Focus on evidence",
        "attachment_ids": [attachment.attachment_id],
    }
    if steering:
        payload.update(
            targets_execution_id=execution.execution_id, steering_intent="refocus"
        )
    before = len(service.list_messages(WORKSPACE, thread.chat_id))
    response = client.post(
        prefix + f"/v1/workspaces/{WORKSPACE}/chats/{thread.chat_id}/messages",
        headers={"Authorization": "Bearer token"},
        json=payload,
    )
    assert response.status_code == 200
    record = service.get_message(WORKSPACE, response.json()["message_id"])
    assert len(service.list_messages(WORKSPACE, thread.chat_id)) == before + 1
    assert accepted == [record]
    assert [message.message_id for message in scheduled] == [record.message_id]
    assert record.actor_id == authenticated_actor_id("token")
    assert record.attachment_ids == (attachment.attachment_id,)
    incoming = from_record(accepted[0])
    assert incoming.direction == "incoming"
    assert incoming.content.source == "user"
    assert incoming.content.text == payload["body"]
    assert incoming.position == record.input_position > 0
    assert bool(response.json().get("steering")) is steering


@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize("alias", [False, True])
def test_steering_effects_precede_first_provider_dispatch(
    prefix: str,
    alias: bool,
    service: ChatService,
    launch: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = service.create_chat(
        WORKSPACE, title="Ordered steering", owner_actor_id="operator"
    )
    execution = launch(thread.chat_id)
    observed: list[int] = []

    class Provider:
        def stream(self, *, message_id=None, **kwargs):
            from tests.thesis_runtime.provider_fakes import completion_frames

            yield from completion_frames(self.complete(**kwargs), message_id)

        streaming = True

        def complete(
            self,
            *,
            messages: Sequence[ProviderMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            current = service.get_execution(WORKSPACE, execution.execution_id)
            observed.append(current.intent_epoch)
            assert current.intent_epoch == 1
            assert len(current.steering_message_ids) == 1
            accepted = service.get_message(WORKSPACE, current.steering_message_ids[0])
            assert accepted.intent_epoch == current.intent_epoch
            assert accepted.actor_id == authenticated_actor_id("token")
            assert from_record(accepted).direction == "incoming"
            inputs = [
                json.loads(message.content)
                for message in messages
                if message.role == "user"
            ]
            assert sum(item["text"] == "Refocus now" for item in inputs) == 1
            return ModelCompletion(
                message=ProviderMessage("assistant", "Acknowledged"),
                model="test",
                provider="test",
                role=role,
                prompt_tokens=0,
                completion_tokens=0,
            )

    conversations = Conversations(
        chat=service, provider=NativeSessionAdapter(Provider())
    )
    # Run the real queued worker inline so premature scheduling is observable.
    monkeypatch.setattr(conversations._workers, "_spawn_thread", lambda work: work())
    scheduled: list[str] = []
    schedule = conversations.schedule_input

    def schedule_input(message: ChatMessage) -> None:
        scheduled.append(message.message_id)
        schedule(message)

    monkeypatch.setattr(conversations, "schedule_input", schedule_input)
    client = TestClient(create_app(bearer_token="token", conversations=conversations))
    payload: dict[str, object] = {"actor_id": "spoofed", "body": "Refocus now"}
    if alias:
        route = f"/v1/workspaces/{WORKSPACE}/executions/{execution.execution_id}/steer"
    else:
        route = f"/v1/workspaces/{WORKSPACE}/chats/{thread.chat_id}/messages"
        payload.update(
            targets_execution_id=execution.execution_id, steering_intent="refocus"
        )
    response = client.post(
        prefix + route, headers={"Authorization": "Bearer token"}, json=payload
    )
    assert response.status_code == 200
    current = service.get_execution(WORKSPACE, execution.execution_id)
    assert scheduled == list(current.steering_message_ids)
    assert observed == [1]
    accepted = service.get_message(WORKSPACE, scheduled[0])
    assert (
        service.read_consumed_input(WORKSPACE, thread.chat_id, "")
        == accepted.input_position
    )
    assert conversations._workers._states == {}


def test_chat_only_intake_is_canonical_without_starting_a_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(bearer_token="token", in_memory=True)
    conversations = app.state.conversations
    assert isinstance(conversations, Conversations)
    spawned: list[object] = []
    monkeypatch.setattr(conversations._workers, "_spawn_thread", spawned.append)
    thread = conversations.chat.create_chat(
        WORKSPACE, title="Durable only", owner_actor_id="operator"
    )
    response = TestClient(app).post(
        f"/api/v1/workspaces/{WORKSPACE}/chats/{thread.chat_id}/messages",
        headers={"Authorization": "Bearer token"},
        json={"actor_id": "spoofed", "body": "Keep this"},
    )
    assert response.status_code == 200
    record = conversations.chat.get_message(WORKSPACE, response.json()["message_id"])
    assert from_record(record).direction == "incoming"
    assert record.input_position > 0
    assert spawned == []
    assert conversations.chat.read_consumed_input(WORKSPACE, thread.chat_id, "") == 0


def test_message_percolates_steering_to_targeted_execution(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    execution = launch(chat["chat_id"])

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={
            "actor_id": "operator",
            "body": "Focus on transaction costs.",
            "targets_execution_id": execution.execution_id,
            "steering_intent": "replan-costs",
        },
    )

    assert response.status_code == 200
    message = response.json()
    assert message["body"] == "Focus on transaction costs."
    assert message["intent_epoch"] == 1
    assert message["execution_id"] == execution.execution_id
    assert message["steering"] == {
        "intent": "replan-costs",
        "targetsExecutionId": execution.execution_id,
        "percolatedTo": [{"executionId": execution.execution_id, "intentEpoch": 1}],
    }

    execution_view = client.get(
        f"/v1/workspaces/{WORKSPACE}/executions/{execution.execution_id}",
        headers={"Authorization": "Bearer thesis-test-token"},
    ).json()
    assert execution_view["intent_epoch"] == 1
    assert message["message_id"] in execution_view["steering_message_ids"]

    events = client.get(
        f"/v1/workspaces/{WORKSPACE}/executions/{execution.execution_id}/events",
        headers={"Authorization": "Bearer thesis-test-token"},
    ).json()["events"]
    steered = events[-1]
    assert steered["eventType"] == "workflow.steered"
    assert steered["intentEpoch"] == 1
    assert steered["payload"]["messageId"] == message["message_id"]
    assert steered["payload"]["steeringIntent"] == "replan-costs"

    transcript = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages",
        headers={"Authorization": "Bearer thesis-test-token"},
    ).json()["items"]
    assert any(item["message_id"] == message["message_id"] for item in transcript)


def test_message_without_target_percolates_to_all_active_executions(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    first = launch(chat["chat_id"])
    second = launch(chat["chat_id"])
    finished = launch(chat["chat_id"])
    assert service.start_workflow(WORKSPACE, finished.execution_id, actor_id="worker")
    service.cancel_workflow(WORKSPACE, finished.execution_id, actor_id="operator")

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={
            "actor_id": "operator",
            "body": "Prioritise the shorter horizon.",
            "steering_intent": "refocus",
        },
    )

    assert response.status_code == 200
    message = response.json()
    assert message["steering"] == {
        "intent": "refocus",
        "targetsExecutionId": None,
        "percolatedTo": [
            {"executionId": first.execution_id, "intentEpoch": 1},
            {"executionId": second.execution_id, "intentEpoch": 1},
        ],
    }
    assert message["execution_id"] is None
    assert message["intent_epoch"] == 1
    assert (
        client.get(
            f"/v1/workspaces/{WORKSPACE}/executions/{finished.execution_id}",
            headers={"Authorization": "Bearer thesis-test-token"},
        ).json()["intent_epoch"]
        == 0
    )


def test_plain_message_does_not_percolate(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    execution = launch(chat["chat_id"])

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"actor_id": "operator", "body": "What is running?"},
    )

    assert response.status_code == 200
    message = response.json()
    assert "steering" not in message
    assert message["intent_epoch"] == 0
    assert (
        client.get(
            f"/v1/workspaces/{WORKSPACE}/executions/{execution.execution_id}",
            headers={"Authorization": "Bearer thesis-test-token"},
        ).json()["intent_epoch"]
        == 0
    )


def test_steering_target_in_terminal_state_is_rejected(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    execution = launch(chat["chat_id"])
    service.start_workflow(WORKSPACE, execution.execution_id, actor_id="worker")
    service.cancel_workflow(WORKSPACE, execution.execution_id, actor_id="operator")

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={
            "actor_id": "operator",
            "body": "Too late.",
            "targets_execution_id": execution.execution_id,
            "steering_intent": "replan",
        },
    )

    assert response.status_code == 409


def test_steering_rejects_target_from_another_chat(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    first = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"title": "First", "owner_actor_id": "operator"},
    ).json()
    second = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"title": "Second", "owner_actor_id": "operator"},
    ).json()
    execution = launch(second["chat_id"])

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{first['chat_id']}/messages",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={
            "actor_id": "operator",
            "body": "Wrong chat.",
            "targets_execution_id": execution.execution_id,
            "steering_intent": "replan",
        },
    )

    assert response.status_code == 409


def test_steering_rejects_non_user_role(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    execution = launch(chat["chat_id"])

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={
            "actor_id": "assistant-bot",
            "body": "Narration cannot steer.",
            "role": "assistant",
            "targets_execution_id": execution.execution_id,
            "steering_intent": "replan",
        },
    )

    assert response.status_code == 422


def test_steering_rejects_conflicting_execution_fields(client: TestClient) -> None:
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={
            "actor_id": "operator",
            "body": "Conflicting target.",
            "execution_id": "execution-999",
            "targets_execution_id": "execution-001",
            "steering_intent": "replan",
        },
    )

    assert response.status_code == 422


def test_deprecated_steer_alias_posts_message_into_chat(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    execution = launch(chat["chat_id"])
    service.start_workflow(WORKSPACE, execution.execution_id, actor_id="worker")

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/executions/{execution.execution_id}/steer",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"actor_id": "operator", "body": "Use lower fees."},
    )

    assert response.status_code == 200
    steered = response.json()
    assert steered["intent_epoch"] == 1
    transcript = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages",
        headers={"Authorization": "Bearer thesis-test-token"},
    ).json()["items"]
    steering_messages = [
        item
        for item in transcript
        if item["message_id"] in steered["steering_message_ids"]
    ]
    assert len(steering_messages) == 1
    assert steering_messages[0]["body"] == "Use lower fees."
    assert steering_messages[0]["execution_id"] == execution.execution_id
    assert steering_messages[0]["intent_epoch"] == 1

    schema = client.get("/openapi.json").json()
    steer_route = schema["paths"][
        "/v1/workspaces/{workspace_id}/executions/{execution_id}/steer"
    ]["post"]
    assert steer_route["deprecated"] is True


def test_control_plane_routes_remain_execution_scoped(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    execution = launch(chat["chat_id"])
    headers = {"Authorization": "Bearer thesis-test-token"}
    base = f"/v1/workspaces/{WORKSPACE}/executions/{execution.execution_id}"

    started = client.post(base + "/start", headers=headers, json={"actor_id": "worker"})
    assert started.status_code == 200
    assert started.json()["state"] == "running"

    stopped = client.post(
        base + "/stop", headers=headers, json={"actor_id": "operator"}
    )
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "stopped"

    cancelled = client.post(
        base + "/cancel", headers=headers, json={"actor_id": "operator"}
    )
    assert cancelled.status_code == 409


def test_messages_require_known_chat(client: TestClient) -> None:
    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/chat-999/messages",
        headers={"Authorization": "Bearer thesis-test-token"},
        json={"actor_id": "operator", "body": "Missing chat."},
    )
    assert response.status_code == 404


def test_chat_execution_route_returns_latest_or_null(
    client: TestClient, launch: Any
) -> None:
    headers = {"Authorization": "Bearer thesis-test-token"}
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers=headers,
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    route = f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/execution"

    assert client.get(route, headers=headers).json() is None

    execution = launch(chat["chat_id"])
    latest = client.get(route, headers=headers)
    assert latest.status_code == 200
    assert latest.json()["execution_id"] == execution.execution_id
    assert latest.json()["chat_id"] == chat["chat_id"]


def test_delete_chat_removes_thread_and_cascades(
    client: TestClient, service: ChatService
) -> None:
    headers = {"Authorization": "Bearer thesis-test-token"}
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers=headers,
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    route = f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}"
    client.post(
        f"{route}/messages",
        headers=headers,
        json={"actor_id": "operator", "body": "Delete me."},
    )

    deleted = client.delete(route, headers=headers)
    assert deleted.status_code == 204
    assert client.get(route, headers=headers).status_code == 404
    service._cleanup_queue.join()
    assert service.persistence.list(ChatMessage, WORKSPACE) == ()

    listing = client.get(f"/v1/workspaces/{WORKSPACE}/chats", headers=headers).json()
    assert listing["items"] == []


def test_messages_endpoint_round_trips_workflow_launch_datum(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    headers = {"Authorization": "Bearer thesis-test-token"}
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers=headers,
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    execution = launch(chat["chat_id"])
    datum = {
        "datumType": "workflow_execution",
        "ok": True,
        "summary": "Launched",
        "text": "",
        "failure": None,
        "structured": {
            "executionId": execution.execution_id,
            "phase": execution.phase,
            "workflowId": execution.workflow_id,
            "version": execution.workflow_version,
            "state": execution.state.value,
        },
    }
    delivered = service.send_message(
        WORKSPACE,
        chat["chat_id"],
        actor_id="thesis-workflow",
        body="Hypothesis development",
        role=MessageRole.TOOL,
        tool_result=datum,
        execution_id=execution.execution_id,
    )
    items = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages", headers=headers
    ).json()["items"]
    match = next(item for item in items if item["message_id"] == delivered.message_id)
    assert match["kind"] == "message"
    assert match["tool_result"] == datum
    assert match["data"]["activityKind"] == "delivery"


def test_chat_routes_require_bearer_token(make_client, service: ChatService) -> None:
    client = make_client(service)
    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        json={"title": "Research", "owner_actor_id": "operator"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
