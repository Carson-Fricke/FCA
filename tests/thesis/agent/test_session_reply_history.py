"""Root turns retain tool evidence without invalid orphaned tool messages."""

import json

from thesis_runtime.adapters.providers.session_adapter import project_native_messages
from thesis_runtime.contracts.messages import decode_transcript
from thesis_runtime.contracts.records import MessageRole
from thesis_runtime.conversations import ChatService


def test_orphaned_result_and_async_failure_remain_visible() -> None:
    chat = ChatService()
    thread = chat.create_chat("workspace-1", title="History", owner_actor_id="operator")
    message = chat.send_message(
        "workspace-1",
        thread.chat_id,
        actor_id="runtime",
        body="Workflow failed",
        role=MessageRole.TOOL,
        tool_result={"ok": False, "structured": {"missing": ["residual_ar1"]}},
    )

    canonical = decode_transcript([message])
    assert canonical[0].direction == "incoming"
    assert canonical[0].content.correlation is None
    seeded = project_native_messages(canonical)

    assert seeded[-1].role == "user"
    payload = json.loads(seeded[-1].content)
    assert payload["text"] == "Workflow failed"
    assert payload["source"] == "tool_result"
    assert payload["result"] == {
        "ok": False,
        "structured": {"missing": ["residual_ar1"]},
    }
    assert payload["correlation"] is None


def test_paired_tool_results_keep_native_tool_roles() -> None:
    from thesis_runtime.contracts.messages import (
        MessageContent,
        SessionMessage,
        ToolCorrelation,
        ToolRequest,
    )

    chat = ChatService()
    thread = chat.create_chat("workspace-1", title="History", owner_actor_id="operator")
    envelope = SessionMessage(
        "envelope",
        "outgoing",
        MessageContent(
            "Checking",
            "agent",
            requests=(ToolRequest("call-1", "validate_study", "{}"),),
        ),
    )
    chat.create_message(
        "workspace-1",
        thread.chat_id,
        actor_id="agent",
        message_id="envelope",
        body="Checking",
        role=MessageRole.ASSISTANT,
        data={"sessionMessage": envelope.as_dict()},
    )
    result_envelope = SessionMessage(
        "result",
        "incoming",
        MessageContent(
            "Valid",
            "validate_study",
            correlation=ToolCorrelation("envelope", "call-1", "validate_study"),
            result={"ok": True, "structured": {"valid": True}},
        ),
    )
    chat.send_message(
        "workspace-1",
        thread.chat_id,
        actor_id="runtime",
        body="Valid",
        role=MessageRole.TOOL,
        data={"sessionMessage": result_envelope.as_dict()},
        tool_result={
            "ok": True,
            "structured": {"valid": True},
            "callId": "call-1",
            "tool": "validate_study",
        },
    )

    canonical = decode_transcript(chat.list_messages("workspace-1", thread.chat_id))
    correlation = canonical[-1].content.correlation
    assert correlation is not None
    assert correlation.request_message_id == "envelope"
    assert correlation.request_id == "call-1"
    assert correlation.tool_name == "validate_study"
    seeded = project_native_messages(canonical)

    assert seeded[-2].tool_calls[0].id == "call-1"
    assert seeded[-1].role == "tool"
    assert seeded[-1].tool_call_id == "call-1"
    payload = json.loads(seeded[-1].content)
    assert payload["result"]["structured"] == {"valid": True}
    assert payload["correlation"] == correlation.as_dict()
