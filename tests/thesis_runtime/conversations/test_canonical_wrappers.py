"""Storage-only chat convenience wrappers share the canonical incoming writer."""

from unittest.mock import Mock

import pytest

from thesis_runtime.adapters.providers.session_adapter import project_native_messages
from thesis_runtime.contracts.activity import decode_activity, is_external_input
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    from_record,
)
from thesis_runtime.contracts.records import MessageRole, WorkflowDefinition
from thesis_runtime.conversations import incoming
from thesis_runtime.conversations.service import ChatService

WORKSPACE = "workspace"
WORKFLOW = WorkflowDefinition("research", "1.0.0", "Research", ("creation",))


@pytest.fixture
def service() -> tuple[ChatService, str]:
    chat = ChatService()
    thread = chat.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    return chat, thread.chat_id


@pytest.mark.parametrize("wrapper", ["send", "launch", "steering"])
def test_wrappers_use_exact_incoming_writer_once(service, monkeypatch, wrapper):
    chat, chat_id = service
    writer = Mock(wraps=incoming.write_incoming)
    storage = Mock(wraps=chat.create_message)
    monkeypatch.setattr(incoming, "write_incoming", writer)
    monkeypatch.setattr(chat, "create_message", storage)
    if wrapper == "send":
        message = chat.send_message(
            WORKSPACE, chat_id, actor_id="operator", body="Research"
        )
    elif wrapper == "launch":
        execution = chat.launch_workflow(
            WORKSPACE, chat_id, actor_id="operator", workflow=WORKFLOW, body="Research"
        )
        message = chat.get_message(WORKSPACE, execution.trigger_message_id)
    else:
        message = chat.post_steering_message(
            WORKSPACE, chat_id, actor_id="operator", body="Research"
        ).message
    writer.assert_called_once()
    storage.assert_called_once()
    assert writer.call_args.args[0] is chat
    assert writer.call_args.args[1].is_root
    assert len(chat.list_messages(WORKSPACE, chat_id)) == 1
    assert message.data is not None and "sessionMessage" in message.data
    canonical = from_record(message)
    assert canonical.message_id == message.message_id
    assert canonical.direction == "incoming"
    assert canonical.position == message.input_position == 1
    assert canonical.content == MessageContent("Research", "user")
    assert is_external_input(message)


@pytest.mark.parametrize(
    ("role", "source", "data"),
    [
        (MessageRole.USER, "user", {"activityKind": "model_output"}),
        (MessageRole.TOOL, "delivery", {"toolCallId": "unmatched"}),
        (MessageRole.SYSTEM, "runtime", {"bootstrapInstruction": True}),
        (MessageRole.USER, "runtime", {"toolActivity": "finished"}),
    ],
)
def test_legacy_metadata_cannot_bypass_intake_or_grant_authority(
    service, role, source, data
):
    chat, chat_id = service
    message = chat.send_message(
        WORKSPACE, chat_id, actor_id="operator", body="Evidence", role=role, data=data
    )
    canonical = from_record(message)
    assert canonical.direction == "incoming"
    assert canonical.position == message.input_position == 1
    assert canonical.content.source == source
    assert project_native_messages((canonical,))[0].role == "user"


def test_scoped_envelope_preserves_identity_content_and_is_idempotent(
    service, monkeypatch
):
    chat, chat_id = service
    envelope = SessionMessage(
        "receipt",
        "incoming",
        MessageContent(
            "Result",
            "delivery",
            correlation=ToolCorrelation("request-message", "request", "tool"),
            result={"datums": [{"answer": 42}]},
        ),
        position=90,
    )
    data = {"sessionMessage": envelope.as_dict(), "subAgentId": "child"}
    writer = Mock(wraps=incoming.write_incoming)
    monkeypatch.setattr(incoming, "write_incoming", writer)
    first = chat.send_message(
        WORKSPACE,
        chat_id,
        actor_id="operator",
        body="Result card",
        role=MessageRole.TOOL,
        data=data,
    )
    second = chat.send_message(
        WORKSPACE,
        chat_id,
        actor_id="operator",
        body="Result card",
        role=MessageRole.TOOL,
        data=data,
    )
    assert writer.call_count == 2
    assert writer.call_args.args[1].sub_agent_id == "child"
    assert first == second
    assert first.message_id == "receipt"
    assert first.input_position == 1
    assert from_record(first).content == envelope.content
    assert decode_activity(first).sub_agent_id == "child"
    assert len(chat.list_messages(WORKSPACE, chat_id)) == 1
    root = chat.send_message(WORKSPACE, chat_id, actor_id="operator", body="Root")
    assert root.input_position == 1


@pytest.mark.parametrize(
    "canonical",
    [
        None,
        "malformed",
        {"message_id": "receipt", "direction": "incoming"},
        SessionMessage(
            "receipt", "instruction", MessageContent("Policy", "user")
        ).as_dict(),
        SessionMessage(
            "receipt", "outgoing", MessageContent("Output", "agent")
        ).as_dict(),
    ],
)
def test_incoming_wrapper_rejects_malformed_or_nonincoming_envelopes(
    service, canonical
):
    chat, chat_id = service
    with pytest.raises(ValueError):
        chat.send_message(
            WORKSPACE,
            chat_id,
            actor_id="operator",
            body="Input",
            data={"sessionMessage": canonical},
        )
    assert chat.list_messages(WORKSPACE, chat_id) == ()


def test_tool_result_is_detached_neutral_content(service):
    chat, chat_id = service
    result = {"datumType": "tool_result", "values": [1, 2]}
    message = chat.send_message(
        WORKSPACE,
        chat_id,
        actor_id="tool",
        body="Result",
        role=MessageRole.TOOL,
        tool_result=result,
    )
    result["values"].append(3)
    canonical = from_record(message)
    assert canonical.direction == "incoming"
    assert canonical.content.result == {"datumType": "tool_result", "values": (1, 2)}
    assert canonical.position == 1


def test_assistant_output_remains_explicit_and_incoming_reasoning_is_rejected(
    service, monkeypatch
):
    chat, chat_id = service
    writer = Mock(side_effect=AssertionError("output is not intake"))
    monkeypatch.setattr(incoming, "write_incoming", writer)
    output = chat.send_message(
        WORKSPACE,
        chat_id,
        actor_id="agent",
        body="Answer",
        role=MessageRole.ASSISTANT,
        reasoning="Output reasoning",
    )
    assert from_record(output).direction == "outgoing"
    assert output.reasoning == "Output reasoning"
    assert output.input_position == 0
    with pytest.raises(ValueError, match="reasoning"):
        chat.send_message(
            WORKSPACE, chat_id, actor_id="operator", body="Input", reasoning="No"
        )
    writer.assert_not_called()


def test_assistant_reasoning_normalizes_provider_control_characters(service):
    chat, chat_id = service
    output = chat.send_message(
        WORKSPACE,
        chat_id,
        actor_id="agent",
        body="Answer",
        role=MessageRole.ASSISTANT,
        reasoning="step\x00 one\r\nstep\x07 two\tend",
    )
    assert output.reasoning == "step one\nstep two\tend"


@pytest.mark.parametrize("reasoning", ["\x00\x1f", " \t\r\n"])
def test_assistant_reasoning_without_printable_text_collapses_to_absent(
    service, reasoning
):
    chat, chat_id = service
    output = chat.send_message(
        WORKSPACE,
        chat_id,
        actor_id="agent",
        body="Answer",
        role=MessageRole.ASSISTANT,
        reasoning=reasoning,
    )
    assert output.reasoning is None


def test_assistant_reasoning_bounds_over_long_traces(service):
    chat, chat_id = service
    output = chat.send_message(
        WORKSPACE,
        chat_id,
        actor_id="agent",
        body="Answer",
        role=MessageRole.ASSISTANT,
        reasoning="x" * 40_000,
    )
    assert output.reasoning == "x" * 32_768


def test_launch_existing_trigger_does_not_redeliver(service, monkeypatch):
    chat, chat_id = service
    trigger = chat.send_message(WORKSPACE, chat_id, actor_id="operator", body="Start")
    writer = Mock(side_effect=AssertionError("existing trigger must not redeliver"))
    monkeypatch.setattr(incoming, "write_incoming", writer)
    execution = chat.launch_workflow(
        WORKSPACE,
        chat_id,
        actor_id="operator",
        workflow=WORKFLOW,
        message_id=trigger.message_id,
        body="Do not duplicate",
    )
    assert execution.trigger_message_id == trigger.message_id
    assert len(chat.list_messages(WORKSPACE, chat_id)) == 1
    writer.assert_not_called()


def test_steering_fallback_commits_effects_before_ready_callback(service, monkeypatch):
    chat, chat_id = service
    execution = chat.launch_workflow(
        WORKSPACE, chat_id, actor_id="operator", workflow=WORKFLOW, body="Start"
    )
    writer = Mock(wraps=incoming.write_incoming)
    monkeypatch.setattr(incoming, "write_incoming", writer)
    observed = []

    def ready(message):
        current = chat.get_execution(WORKSPACE, execution.execution_id)
        assert current.intent_epoch == message.intent_epoch == 1
        assert current.steering_message_ids == (message.message_id,)
        assert message.execution_id == execution.execution_id
        assert from_record(message).content.source == "user"
        assert message.input_position == 2
        observed.append(message)

    result = chat.post_steering_message(
        WORKSPACE,
        chat_id,
        actor_id="operator",
        body="Refocus",
        targets_execution_id=execution.execution_id,
        after_delivery=ready,
    )
    writer.assert_called_once()
    assert observed == [result.message]
