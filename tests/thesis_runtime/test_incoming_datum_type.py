"""Canonical incoming deliveries preserve the typed datum the model received.

The live UI selects a tool card and the workflow anchor by the persisted
``tool_result.datumType``; a generic ``tool`` there hid the ``workflow_execution``
handle and left the workflow card stuck on the launcher's synthetic phase.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from thesis_runtime.adapters.filesystem.records import FileChatPersistence
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    from_record,
)
from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.delivery import Anchor


def _conversations() -> tuple[ChatService, Conversations]:
    chat = ChatService()
    chat.create_chat("workspace", title="Delivery", owner_actor_id="operator")
    return chat, Conversations(chat=chat)


def _deliver(
    result: dict[str, object], correlation: ToolCorrelation | None
) -> dict[str, object]:
    chat, conversations = _conversations()
    thread = chat.list_chats("workspace")[0]
    durable = conversations.deliver_session_message(
        Anchor("workspace", thread.chat_id, "agent-1"),
        SessionMessage(
            "message-typed-result",
            "incoming",
            MessageContent("Compiled", "tool", correlation=correlation, result=result),
        ),
        schedule=False,
    )
    record = chat.get_message("workspace", durable.message_id)
    assert record.tool_result is not None
    return dict(record.tool_result)


def test_outer_tool_result_keeps_the_primary_datum_type() -> None:
    datum = {
        "datumType": "workflow_execution",
        "ok": True,
        "structured": {"executionId": "execution-1", "state": "running"},
        "text": "Workflow started.",
        "failure": None,
        "summary": None,
    }
    result = _deliver(
        {
            "ok": True,
            "text": "Workflow started.",
            "structured": dict(datum["structured"]),
            "failure": None,
            "datums": [datum],
        },
        ToolCorrelation("request-message", "call-1", "start_workflow"),
    )
    assert result["datumType"] == "workflow_execution"
    assert result["tool"] == "start_workflow"
    assert result["callId"] == "call-1"


def test_datumless_result_keeps_the_generic_source() -> None:
    result = _deliver(
        {"ok": True, "text": "Plain outcome", "structured": {}, "failure": None},
        None,
    )
    assert result["datumType"] == "tool"


def test_structured_datums_survive_restart_and_replay(tmp_path: Path) -> None:
    chat = ChatService(FileChatPersistence(tmp_path))
    thread = chat.create_chat("workspace", title="Delivery", owner_actor_id="operator")
    datums = [
        {
            "datumType": "read_sdk_reference",
            "ok": True,
            "structured": {"topic": "study", "source": "export_study_program!"},
            "text": "",
            "failure": None,
            "summary": None,
        },
        {
            "datumType": "read_artifact",
            "ok": True,
            "structured": {"job_id": "study-1", "name": "metrics.json"},
            "text": "",
            "failure": None,
            "summary": None,
        },
    ]
    durable = Conversations(chat=chat).deliver_session_message(
        Anchor("workspace", thread.chat_id, "agent-1"),
        SessionMessage(
            "message-structured-results",
            "incoming",
            MessageContent(
                "",
                "tool",
                correlation=ToolCorrelation(
                    "request-message", "call-2", "read_sdk_reference"
                ),
                result={"ok": True, "datums": datums},
            ),
        ),
        schedule=False,
    )
    reloaded = ChatService(FileChatPersistence(tmp_path))
    record = reloaded.get_message("workspace", durable.message_id)
    assert record is not None
    assert record.data is not None
    assert "sessionMessage" in record.data
    replayed = from_record(record)
    assert replayed.content.result is not None
    replayed_datums = cast(
        tuple[Mapping[str, object], ...], replayed.content.result["datums"]
    )
    assert [
        dict(cast(Mapping[str, object], datum["structured"]))
        for datum in replayed_datums
    ] == [
        datums[0]["structured"],
        datums[1]["structured"],
    ]
