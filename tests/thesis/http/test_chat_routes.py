"""HTTP tests for chat-owned messaging and percolated steering."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from thesis.chat import ChatMessage, ChatService, MessageRole, TranscriptItemKind

WORKSPACE = "workspace-a"


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
    assert service.persistence.list(ChatMessage, WORKSPACE) == ()

    listing = client.get(f"/v1/workspaces/{WORKSPACE}/chats", headers=headers).json()
    assert listing["items"] == []


def test_messages_endpoint_round_trips_workflow_anchor(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    headers = {"Authorization": "Bearer thesis-test-token"}
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers=headers,
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    execution = launch(chat["chat_id"])
    anchor = service.create_message(
        WORKSPACE,
        chat["chat_id"],
        actor_id="thesis-workflow",
        body="Hypothesis development",
        role=MessageRole.SYSTEM,
        kind=TranscriptItemKind.WORKFLOW,
        data={
            "currentPhase": execution.phase,
            "executionId": execution.execution_id,
            "status": execution.state.value,
            "title": execution.workflow_id,
        },
        execution_id=execution.execution_id,
    )

    items = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages",
        headers=headers,
    ).json()["items"]
    match = next(item for item in items if item["message_id"] == anchor.message_id)
    assert match["kind"] == "workflow"
    assert match["role"] == "system"
    assert match["execution_id"] == execution.execution_id
    assert match["data"]["executionId"] == execution.execution_id


def test_chat_routes_require_bearer_token(make_client, service: ChatService) -> None:
    client = make_client(service)
    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        json={"title": "Research", "owner_actor_id": "operator"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
