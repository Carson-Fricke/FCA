"""Generated schemas match literal record, tree, and timeline response payloads."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from thesis.http.application import create_app
from thesis.http.schema import export_schema, main
from thesis.http.wire import (
    ChatMessageListWire,
    ChatMessageWire,
    ChatThreadListWire,
    ChatThreadWire,
    NodeInputReceiptWire,
    SteeringPercolationWire,
    TimelineItemWire,
    TimelinePageWire,
    WorkflowDefinitionWire,
)
from thesis_runtime.adapters.cosmos.timeline import CosmosTimeline
from thesis_runtime.contracts.activity import ExternalInput, activity_data
from thesis_runtime.contracts.datum import ToolResultWire, tool_result_payload
from thesis_runtime.contracts.records import (
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
)
from thesis_runtime.conversations import ChatService, MessageRole, WorkflowDefinition
from thesis_runtime.execution.events import (
    ActorScope,
    ExecutionEvent,
    ExecutionEventType,
)
from thesis_runtime.subagents.tree import (
    ChatSubAgentTreeWire,
    SubAgentNodeWire,
    project_chat_sub_agents,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
HEADERS = {"Authorization": "Bearer token"}


def validate(schema: dict[str, Any], name: str, payload: object) -> None:
    models = {
        "ChatMessageWire": ChatMessageWire,
        "ChatMessageListWire": ChatMessageListWire,
        "ChatSubAgentTreeWire": ChatSubAgentTreeWire,
        "SubAgentNodeWire": SubAgentNodeWire,
        "TimelinePageWire": TimelinePageWire,
        "TimelineItemWire": TimelineItemWire,
        "ToolResultWire": ToolResultWire,
        "WorkflowDefinitionWire": WorkflowDefinitionWire,
        "ChatThreadWire": ChatThreadWire,
        "ChatThreadListWire": ChatThreadListWire,
        "SteeringPercolationWire": SteeringPercolationWire,
        "NodeInputReceiptWire": NodeInputReceiptWire,
    }
    assert name in schema["components"]["schemas"]
    TypeAdapter(models[name]).validate_python(payload)


def test_export_is_deterministic_complete_and_has_no_dangling_refs(
    wire_schema: dict[str, Any],
) -> None:
    schema = wire_schema
    assert schema == export_schema()
    assert schema["openapi"] == "3.1.0"
    components = schema["components"]["schemas"]
    required = {
        "ErrorResponse",
        "CreateMessageRequest",
        "ChatMessageWire",
        "ChatMessageListWire",
        "SubAgentNodeWire",
        "ChatSubAgentTreeWire",
        "SubAgentNodeListWire",
        "RuntimeActivityWire",
        "DatumWire",
        "ToolResultWire",
        "TimelineItemWire",
        "TimelinePageWire",
        "WorkflowDefinitionWire",
        "CreateMessageResponseWire",
        "ChatThreadWire",
        "ChatThreadListWire",
        "SteeringPercolationWire",
        "NodeInputReceiptWire",
    }
    assert required <= components.keys()

    def check(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "$ref":
                    assert isinstance(item, str)
                    assert item.startswith("#/components/schemas/")
                    assert item.rsplit("/", 1)[-1] in components
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    check(schema)
    for prefix in ("", "/api"):
        path = prefix + "/v1/workspaces/{workspace_id}/chats/{chat_id}"
        for suffix, component in (
            ("/sub_agents", "ChatSubAgentTreeWire"),
            ("/sub_agents/{sub_agent_id}", "SubAgentNodeWire"),
            ("/sub_agents/{sub_agent_id}/sub_agents", "SubAgentNodeListWire"),
            ("/timeline", "TimelinePageWire"),
            ("/messages", "ChatMessageListWire"),
        ):
            response = schema["paths"][path + suffix]["get"]["responses"]["200"]
            assert (
                response["content"]["application/json"]["schema"]["$ref"]
                == f"#/components/schemas/{component}"
            )
        stream = schema["paths"][path + "/timeline/stream"]["get"]["responses"]["200"][
            "content"
        ]
        assert stream["text/event-stream"]["x-thesis-event-schema"] == {
            "$ref": "#/components/schemas/TimelineItemWire"
        }
    assert "ChatMessageRecordWire" not in components


def test_thread_steering_and_node_receipts_preserve_actual_service_data(
    monkeypatch: pytest.MonkeyPatch,
    wire_schema: dict[str, Any],
) -> None:
    from thesis_runtime.conversations.conversation import Conversations

    service = ChatService(clock=lambda: NOW)
    conversations = Conversations(
        chat=service,
        read_consumed_input=service.read_consumed_input,
        acknowledge_input=service.acknowledge_input,
    )
    monkeypatch.setattr(conversations, "wake", lambda anchor: None)
    # Schema coverage admits real inputs but leaves worker/ack execution to wiring tests.
    monkeypatch.setattr(conversations, "schedule", lambda *args, **kwargs: None)
    app = create_app(bearer_token="token", conversations=conversations)
    client = TestClient(app)
    schema = wire_schema
    path = "/v1/workspaces/workspace/chats"
    response = client.post(
        "/api" + path,
        headers=HEADERS,
        json={"title": "Receipts", "owner_actor_id": "operator"},
    )
    assert response.status_code == 200
    thread = response.json()
    validate(schema, "ChatThreadWire", thread)
    chat_id = thread["chat_id"]
    for prefix in ("", "/api"):
        current = client.get(prefix + path + "/" + chat_id, headers=HEADERS).json()
        assert current == service.get_chat("workspace", chat_id).as_dict()
        validate(schema, "ChatThreadWire", current)
        listing = client.get(prefix + path, headers=HEADERS).json()
        validate(schema, "ChatThreadListWire", listing)
        assert listing == {"items": [current]}
    execution = service.launch_workflow(
        "workspace",
        chat_id,
        workflow=WorkflowDefinition(
            workflow_id="flow", version="1.0.0", title="Flow", phases=("work",)
        ),
        actor_id="operator",
        body="Run",
    )
    record = SubAgentRecord(
        workspace_id="workspace",
        created_at=NOW,
        created_by="operator",
        sub_agent_id="agent",
        chat_id=chat_id,
        role="research",
        kind=SubAgentKind.CONVERSATION,
        status=SubAgentStatus.RUNNING,
        accepts_input=True,
        conversation_id="agent",
        root_conversation_id=chat_id,
        parent_conversation_id=chat_id,
    )
    service.persistence.save(record)
    for prefix in ("", "/api"):
        response = client.post(
            prefix + path + "/" + chat_id + "/messages",
            headers=HEADERS,
            json={
                "actor_id": "operator",
                "body": "Steer",
                "targets_execution_id": execution.execution_id,
                "steering_intent": "refocus",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        validate(schema, "SteeringPercolationWire", payload["steering"])
        assert (
            payload["steering"]["percolatedTo"][0]["intentEpoch"]
            == service.get_execution("workspace", execution.execution_id).intent_epoch
        )
        response = client.post(
            prefix + path + "/" + chat_id + "/sub_agents/agent/messages",
            headers=HEADERS,
            json={"body": "Node input"},
        )
        assert response.status_code == 200
        receipt = response.json()
        validate(schema, "NodeInputReceiptWire", receipt)
        message = service.get_message("workspace", receipt["message_id"])
        assert receipt == message.as_dict() | {
            "subAgentId": "agent",
            "operatorPolicy": receipt["operatorPolicy"],
        }
        assert receipt["operatorPolicy"]["nodeId"] == "agent"
        assert receipt["operatorPolicy"]["allowed"] is True
    for prefix in ("", "/api"):
        archived = client.post(
            prefix + path + "/" + chat_id + "/archive", headers=HEADERS
        )
        assert archived.status_code == 200
        validate(schema, "ChatThreadWire", archived.json())


def test_schema_output_file_and_stdout_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    wire_schema: dict[str, Any],
) -> None:
    monkeypatch.setattr(sys, "argv", ["schema"])
    main()
    stdout = capsys.readouterr().out
    output = tmp_path / "backend-wire.json"
    monkeypatch.setattr(sys, "argv", ["schema", "--output", str(output)])
    main()
    assert output.read_text(encoding="utf-8") == stdout
    assert json.loads(stdout) == wire_schema


def test_literal_message_and_extension_metadata_round_trip(
    wire_schema: dict[str, Any],
) -> None:
    service = ChatService(clock=lambda: NOW)
    chat = service.create_chat("workspace", title="Schema", owner_actor_id="operator")
    outcome = tool_result_payload(
        datum_type="tool_result",
        call_id="call",
        ok=False,
        failure={"kind": "compiler"},
        structured={"diagnostic": "repair"},
        summary=None,
        text="Failed",
        tool="validate_strategy",
    )
    message = service.send_message(
        "workspace",
        chat.chat_id,
        actor_id="operator",
        role=MessageRole.TOOL,
        body="Outcome",
        data=activity_data(ExternalInput())
        | {"ownerActorId": "operator", "custom": {"stored": True}, "nullable": None},
        tool_result=outcome,
    )
    app = create_app(bearer_token="token", chat_service=service)
    response = TestClient(app).get(
        f"/api/v1/workspaces/workspace/chats/{chat.chat_id}/messages", headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json() == {"items": [message.as_dict()]}
    schema = wire_schema
    validate(schema, "ChatMessageWire", message.as_dict())
    validate(schema, "ChatMessageListWire", response.json())
    validate(schema, "ToolResultWire", outcome)


def test_real_tree_and_all_timeline_variants_match_export(
    wire_schema: dict[str, Any],
) -> None:
    service = ChatService(clock=lambda: NOW)
    chat = service.create_chat("workspace", title="Schema", owner_actor_id="operator")
    message = service.send_message(
        "workspace", chat.chat_id, actor_id="operator", body="Start"
    )
    execution = service.launch_workflow(
        "workspace",
        chat.chat_id,
        workflow=WorkflowDefinition(
            workflow_id="flow", version="1.0.0", title="Flow", phases=("work",)
        ),
        actor_id="operator",
        body="Run",
    )
    event = ExecutionEvent(
        execution_id=execution.execution_id,
        workspace_id="workspace",
        event_type=ExecutionEventType.WORKFLOW_STARTED,
        correlation_id="corr",
        actor_id="operator",
        actor_scope=ActorScope.USER,
        occurred_at=NOW,
    )
    record = SubAgentRecord(
        workspace_id="workspace",
        created_at=NOW,
        created_by="operator",
        sub_agent_id="agent",
        chat_id=chat.chat_id,
        role="research",
        kind=SubAgentKind.CONVERSATION,
        status=SubAgentStatus.RUNNING,
        phase_attempt_id="attempt",
        spec_version="2.0.0",
        conversation_id="agent",
        root_conversation_id=chat.chat_id,
        parent_conversation_id=chat.chat_id,
    )
    service.persistence.save(record)
    tree = project_chat_sub_agents(
        [record], workspace_id="workspace", chat_id=chat.chat_id
    )
    container = Mock(spec=ContainerProxy)
    timeline = CosmosTimeline(cast(ContainerProxy, container))
    counter: dict[str, Any] | None = None
    stored: dict[str, dict[str, Any]] = {}

    def read_item(*, item: str, partition_key: str) -> dict[str, Any]:
        if counter is not None and item == counter["id"]:
            return dict(counter)
        raise CosmosResourceNotFoundError(status_code=404)

    def batch(*, batch_operations: list[Any], partition_key: str) -> None:
        nonlocal counter
        counter = dict(batch_operations[0][1][-1]) | {"_etag": "etag"}
        document = batch_operations[1][1][0]
        stored[document["id"]] = timeline.hydrate(document)

    container.read_item.side_effect = read_item
    container.execute_item_batch.side_effect = batch
    for kind, payload in (
        ("message", message.as_dict()),
        ("event", event.as_dict()),
        ("execution", execution.as_dict()),
    ):
        timeline.append(
            chat_id=chat.chat_id,
            workspace_id="workspace",
            kind=kind,
            payload=payload,
            item_id=kind,
            thread_id="root",
            execution_id=execution.execution_id,
            occurred_at=NOW.isoformat(),
        )
    container.query_items.return_value = list(reversed(list(stored.values())))
    app = create_app(
        bearer_token="token",
        chat_service=service,
        timeline=timeline,
        stream_max_poll_cycles=1,
    )
    client = TestClient(app)
    schema = wire_schema
    path = f"/v1/workspaces/workspace/chats/{chat.chat_id}"
    response = client.get(path + "/sub_agents", headers=HEADERS)
    assert response.json() == tree.as_dict()
    validate(schema, "ChatSubAgentTreeWire", response.json())
    node = client.get(path + "/sub_agents/agent", headers=HEADERS).json()
    validate(schema, "SubAgentNodeWire", node)
    assert node["phaseAttemptId"] == "attempt"
    assert node["specVersion"] == "2.0.0"
    for prefix in ("", "/api"):
        response = client.get(prefix + path + "/timeline", headers=HEADERS)
        assert response.status_code == 200
        validate(schema, "TimelinePageWire", response.json())
        assert response.json()["items"] == list(stored.values())
        for item in response.json()["items"]:
            validate(schema, "TimelineItemWire", item)
        stream = client.get(prefix + path + "/timeline/stream", headers=HEADERS)
        assert stream.status_code == 200
        frames = [
            json.loads(line.removeprefix("data: "))
            for line in stream.text.splitlines()
            if line.startswith("data: ")
        ]
        # The mocked read-after also returns the same durable items for this bounded stream.
        assert frames
        for item in frames:
            assert item in stored.values()
            validate(schema, "TimelineItemWire", item)
    definition = client.get(
        "/api/v1/workflows/hypothesis-development", headers=HEADERS
    ).json()
    validate(schema, "WorkflowDefinitionWire", definition)
