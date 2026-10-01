"""Root turns retain tool evidence without invalid orphaned tool messages."""

from thesis.agent.session_reply import _seed_messages
from thesis.chat import ChatService
from thesis.chat.contracts import MessageRole


def test_orphaned_result_and_async_failure_remain_visible() -> None:
    chat = ChatService()
    thread = chat.create_chat("workspace-1", title="History", owner_actor_id="operator")
    message = chat.create_message(
        "workspace-1",
        thread.chat_id,
        actor_id="runtime",
        body="Workflow failed",
        role=MessageRole.TOOL,
        tool_result={"ok": False, "structured": {"missing": ["residual_ar1"]}},
    )

    seeded = _seed_messages([message], "System")

    assert seeded[-1].role == "user"
    assert "residual_ar1" in seeded[-1].content
    assert "Workflow failed" in seeded[-1].content
    assert '"ok":false' in seeded[-1].content


def test_paired_tool_results_keep_native_tool_roles() -> None:
    chat = ChatService()
    thread = chat.create_chat("workspace-1", title="History", owner_actor_id="operator")
    envelope = chat.create_message(
        "workspace-1",
        thread.chat_id,
        actor_id="agent",
        body="Checking",
        role=MessageRole.ASSISTANT,
        data={
            "toolCalls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "validate_study", "arguments": "{}"},
                }
            ]
        },
    )
    result = chat.create_message(
        "workspace-1",
        thread.chat_id,
        actor_id="runtime",
        body="Valid",
        role=MessageRole.TOOL,
        data={"toolCallId": "call-1"},
        tool_result={"ok": True, "structured": {"valid": True}},
    )

    seeded = _seed_messages([envelope, result], "System")

    assert seeded[-2].tool_calls[0].id == "call-1"
    assert seeded[-1].role == "tool"
    assert seeded[-1].tool_call_id == "call-1"
    assert '"valid":true' in seeded[-1].content
