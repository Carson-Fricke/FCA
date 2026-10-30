"""Outline reads remain scoped, paged, and independent of transcript bodies."""

from typing import cast
from unittest.mock import Mock

from azure.cosmos import ContainerProxy
from fastapi.testclient import TestClient

from thesis.http.application import create_app
from thesis.http.conversation_outline import (
    OUTLINE_METADATA_ARRAYS,
    OUTLINE_METADATA_FIELDS,
)
from thesis_runtime.adapters.cosmos.timeline import CosmosTimeline
from thesis_runtime.adapters.receipt_projection import receipt_header
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
)
from thesis_runtime.contracts.records import MessageRole
from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations


def test_outline_pages_metadata_without_exposing_artifact_content(service: ChatService):
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    turn = service.send_message(
        "workspace-a", chat.chat_id, actor_id="operator", body="Explore MSFT"
    )
    receipt = service.send_message(
        "workspace-a",
        chat.chat_id,
        actor_id="tool",
        role=MessageRole.TOOL,
        body="Read metrics",
        tool_result={
            "datumType": "read_artifact",
            "ok": True,
            "structured": {
                "job_id": "job-1",
                "name": "metrics.json",
                "content": "private large body",
            },
            "text": "private large body",
            "summary": "Read metrics",
            "failure": None,
        },
    )
    bundle = (
        Conversations(chat=service)
        .root("workspace-a", chat.chat_id)
        .deliver_session_message(
            SessionMessage(
                "bundled-result",
                "incoming",
                MessageContent(
                    "Workflow and study",
                    "tool",
                    correlation=ToolCorrelation(
                        "request-message", "request-1", "research"
                    ),
                    result={
                        "ok": True,
                        "datums": [
                            {
                                "datumType": "workflow_execution",
                                "ok": True,
                                "structured": {
                                    "executionId": "execution-1",
                                    "workflowId": "research_investigation",
                                    "state": "running",
                                },
                            },
                            {
                                "datumType": "run_study",
                                "ok": True,
                                "structured": {
                                    "job_id": "job-2",
                                    "programId": "momentum",
                                    "content": "private large body",
                                },
                            },
                        ],
                    },
                ),
            ),
            schedule=False,
        )
    )
    documents = [{"kind": "output", "seq": seq} for seq in range(1, 501)] + [
        {"kind": "message", "seq": 501, "threadId": "root", "payload": turn.as_dict()},
        {
            "kind": "message",
            "seq": 502,
            "threadId": "nested-critique",
            "payload": receipt.as_dict(),
        },
        {
            "kind": "message",
            "seq": 503,
            "threadId": "root",
            "payload": bundle.as_dict(),
        },
    ]
    documents = [
        {**item, "workspaceId": "workspace-a", "chatId": chat.chat_id}
        for item in documents
    ]
    container = Mock(spec=ContainerProxy)
    container.read_item.return_value = {"nextSeq": 503}

    def query_items(*, query, parameters, **kwargs):
        values = {parameter["name"]: parameter["value"] for parameter in parameters}
        return iter(
            [
                receipt_header(item, OUTLINE_METADATA_FIELDS, OUTLINE_METADATA_ARRAYS)
                for item in documents
                if item["kind"] == "message"
                if item["seq"] > values.get("@afterSeq", values.get("@after", 0))
            ][: values.get("@limit", 500)]
        )

    container.query_items.side_effect = query_items
    timeline = CosmosTimeline(cast(ContainerProxy, container))
    with TestClient(
        create_app(bearer_token="token", chat_service=service, timeline=timeline)
    ) as client:
        headers = {"Authorization": "Bearer token"}
        url = f"/v1/workspaces/workspace-a/chats/{chat.chat_id}/outline"
        first = client.get(url, headers=headers).json()
        assert first["nextAfter"] is None and first["after"] == 503
        second = client.get(url, headers=headers)
        assert second.status_code == 200
        page = second.json()
        assert page["nextAfter"] is None and page["after"] == 503
        assert [(item["kind"], item["title"]) for item in page["items"]] == [
            ("turn", "Explore MSFT"),
            ("artifact", "metrics.json"),
            ("workflow", "research_investigation"),
            ("study", "momentum"),
        ]
        artifact = page["items"][1]
        assert artifact["threadId"] == "nested-critique"
        assert artifact["messageId"] == receipt.message_id
        assert artifact["datumIndex"] is None
        workflow, study = page["items"][2:]
        assert workflow["entityId"] == "execution-1"
        assert workflow["datumIndex"] == 0 and workflow["status"] == "running"
        assert study["entityId"] == "job-2"
        assert study["datumIndex"] == 1 and study["status"] == "submitted"
        assert study["requestMessageId"] == "request-message"
        assert "private large body" not in second.text
        assert (
            client.get(url.replace("workspace-a", "other"), headers=headers).status_code
            == 404
        )
        assert client.get(url).status_code == 401
