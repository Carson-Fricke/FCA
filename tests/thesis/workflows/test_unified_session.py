"""Workflow phase agents use the same durable writer as chat roots."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

from thesis.agent.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    decode_transcript,
)
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.delivery import Anchor
from thesis.agent.runtime.session import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.chat import ChatService
from thesis.chat.contracts import MessageRole, SubAgentKind, WorkflowExecution
from thesis.providers.invocations import ChatMessage, ModelCompletion, Role, ToolCall
from thesis.providers.session_adapter import project_native_messages


def test_root_and_workflow_phase_have_identical_durable_tool_protocol() -> None:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Unified", owner_actor_id="operator")
    contexts: list[ToolContext] = []
    requests: list[tuple[ChatMessage, ...]] = []
    profiles: list[Role] = []

    class Provider:
        def complete(
            self,
            *,
            messages: Sequence[ChatMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            requests.append(tuple(messages))
            profiles.append(role)
            return ModelCompletion(
                "Checking" if len(requests) % 2 else "Finished",
                "model",
                "provider",
                role,
                0,
                0,
                tool_calls=(ToolCall("call", "check", "{}"),)
                if len(requests) % 2
                else (),
            )

    def registry(context: ToolContext) -> SessionToolRegistry:
        contexts.append(context)

        def handler(arguments: Mapping[str, object]) -> ToolExecution:
            anchor = Anchor(context.workspace_id, context.chat_id, context.sub_agent_id)
            envelope = service.transcript(anchor)[-1]
            assert envelope.role is MessageRole.ASSISTANT
            assert envelope.data is not None and envelope.data["toolCalls"]
            return ToolExecution(ok=True, text="Checked")

        return SessionToolRegistry(
            [
                RegisteredTool(
                    {"type": "function", "function": {"name": "check"}}, handler
                ),
            ]
        )

    service = Conversations(chat=chat, provider=Provider(), registry_factory=registry)
    chat.persistence.save(
        WorkflowExecution(
            workspace_id="workspace",
            execution_id="execution",
            chat_id=thread.chat_id,
            trigger_message_id="trigger",
            workflow_id="research",
            workflow_version="1.0.0",
            requested_by="operator",
            created_by="operator",
            created_at=datetime.now(UTC),
        )
    )
    phase = service.lifecycle.spawn(
        "workspace",
        thread.chat_id,
        actor_id="operator",
        spec=SubAgentSpec(
            role="research", phase="creation", kind=SubAgentKind.ONE_SHOT
        ),
        phase_attempt_id="attempt",
        workflow_execution_id="execution",
    )
    for anchor in (
        Anchor("workspace", thread.chat_id),
        Anchor("workspace", thread.chat_id, phase.sub_agent_id),
    ):
        chat.send_message(
            "workspace",
            thread.chat_id,
            actor_id="operator",
            body="Check",
            data={"subAgentId": anchor.sub_agent_id} if anchor.sub_agent_id else None,
        )
        result = service.run_node_turn(
            "workspace", thread.chat_id, anchor.sub_agent_id or ""
        )
        assert result.status == "completed"
        durable = service.transcript(anchor)
        assert [message.role for message in durable] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.TOOL,
            MessageRole.ASSISTANT,
        ]
        assert durable[2].data is not None
        assert durable[2].data["toolCallId"] == "call"
        assert durable[2].data["arguments"] == {}
        assert requests[-1][-1].tool_call_id == "call"
        canonical = decode_transcript(durable)
        assert [message.direction for message in canonical] == [
            "incoming",
            "outgoing",
            "incoming",
            "outgoing",
        ]
        assert canonical[0].content == MessageContent("Check", "user")
        assert canonical[1].content.source == "agent"
        assert canonical[1].content.text == "Checking"
        assert canonical[2].content.source == "tool"
        assert canonical[2].content.text == "Checked"
        assert canonical[2].content.correlation == ToolCorrelation(
            canonical[1].message_id, "call", "check"
        )
        assert json.loads(requests[-1][-1].content)["result"]["ok"] is True
        assert requests[-1][1:] == project_native_messages(canonical[:-1])
    root_contexts = [context for context in contexts if context.sub_agent_id is None]
    phase_contexts = [
        context for context in contexts if context.sub_agent_id == phase.sub_agent_id
    ]
    assert root_contexts and phase_contexts
    assert all(context.phase_attempt_id is None for context in root_contexts)
    assert all(context.phase_attempt_id == "attempt" for context in phase_contexts)
    assert all(
        context.workflow_execution_id == "execution" for context in phase_contexts
    )
    assert profiles == ["primary"] * len(requests)
    current = service.lifecycle.get("workspace", phase.sub_agent_id)
    assert current is not None and current.status.value == "running"


@pytest.mark.parametrize("source", ["user", "tool", "sub_agent_status"])
def test_fresh_live_and_replay_inputs_share_exact_projection(source: str) -> None:
    chat = ChatService()
    thread = chat.create_chat(
        "workspace", title="Projection", owner_actor_id="operator"
    )
    anchor = Anchor("workspace", thread.chat_id)
    requests: list[tuple[ChatMessage, ...]] = []
    incoming = (
        SessionMessage(
            "fresh",
            "incoming",
            MessageContent("Fresh evidence", source, result={"ok": True}),
        ),
        SessionMessage(
            "live",
            "incoming",
            MessageContent("Live evidence", source, result={"ok": False}),
        ),
    )

    class Provider:
        def complete(
            self,
            *,
            messages: Sequence[ChatMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            requests.append(tuple(messages))
            return ModelCompletion(
                "Checking" if len(requests) == 1 else "Finished",
                "model",
                "provider",
                role,
                0,
                0,
                tool_calls=(ToolCall("call", "check", "{}"),)
                if len(requests) == 1
                else (),
            )

    def handler(arguments: Mapping[str, object]) -> ToolExecution:
        service.deliver_session_message(anchor, incoming[1], schedule=False)
        return ToolExecution(ok=True, text="Checked")

    service = Conversations(
        chat=chat,
        provider=Provider(),
        registry_factory=lambda context: SessionToolRegistry(
            [
                RegisteredTool(
                    {"type": "function", "function": {"name": "check"}}, handler
                )
            ]
        ),
    )
    service.deliver_session_message(anchor, incoming[0], schedule=False)
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "completed"
    canonical = decode_transcript(service.transcript(anchor))
    assert [
        message.message_id
        for message in canonical
        if message.message_id in {"fresh", "live"}
    ] == ["fresh", "live"]
    assert requests[0][0] == requests[1][0]
    assert requests[0][1:] == project_native_messages(canonical[:1])
    assert requests[1][1:] == project_native_messages(canonical[:-1])
    assert requests[1][-2].role == "tool"
    assert requests[1][-1].role == "user"
    for native, message in (
        (requests[0][-1], incoming[0]),
        (requests[1][-1], incoming[1]),
    ):
        assert json.loads(native.content) == {
            "text": message.content.text,
            "source": source,
            "result": {"ok": message.message_id == "fresh"},
            "correlation": None,
        }
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "completed"
    assert requests[2][0] == requests[1][0]
    assert requests[2][1:] == project_native_messages(canonical)
    assert requests[2][:-1] == requests[1]
