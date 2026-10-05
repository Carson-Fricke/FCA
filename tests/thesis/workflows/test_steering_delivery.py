"""Public workflow steering uses canonical root intake before its single wake."""

import json
from datetime import UTC, datetime
from typing import Any, Never
from unittest.mock import Mock

import pytest

from thesis.agent.messages import from_record
from thesis.agent.runtime.activity import decode_activity
from thesis.agent.runtime.conversation import Conversations
from thesis.chat.contracts import (
    ChatMessage,
    InvalidLifecycleTransition,
    MessageRole,
    RecordNotFound,
    WorkflowDefinition,
    WorkflowExecution,
)
from thesis.chat.service import ChatService
from thesis.execution.events import ExecutionEventType
from thesis.providers.session_adapter import project_native_messages
from thesis.workflows.core.execution_store import InMemoryExecutionStore
from thesis.workflows.core.phase import ConversationPhase, TerminalPhase
from thesis.workflows.core.spec import WorkflowSpec
from thesis.workflows.core.workflow import Workflow, WorkflowDoctrine, WorkflowPorts

NOW = datetime(2026, 10, 3, tzinfo=UTC)


def _unused(*args: object, **kwargs: object) -> Never:
    pytest.fail("steering must not execute a workflow phase")


@pytest.fixture
def runtime() -> tuple[
    Workflow[Any, Any, Any], ChatService, Conversations, WorkflowExecution
]:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Steering", owner_actor_id="operator")
    provider = Mock()
    provider.complete.side_effect = AssertionError("steering must not call a provider")
    conversations = Conversations(chat=chat, provider=provider)
    trigger = conversations.root("workspace", thread.chat_id).send(
        "Research", actor_id="operator", schedule=False
    )
    definition = WorkflowDefinition(
        "research", "1.0.0", "Research", ("creation", "done")
    )
    execution = chat.launch_workflow(
        "workspace",
        thread.chat_id,
        workflow=definition,
        actor_id="operator",
        message_id=trigger.message_id,
    )
    execution = chat.start_workflow(
        "workspace", execution.execution_id, actor_id="operator"
    )
    doctrine = WorkflowDoctrine(
        definition=definition,
        initial_phase="creation",
        actor="workflow-agent",
        active_phases=("creation",),
        conversational_phases=frozenset({"creation"}),
        orchestrators={},
        tasks={},
        activity={},
        default_activity="research",
        mcp_tools={},
        request_of=_unused,
        context_of=_unused,
        project=_unused,
        reconstruct=_unused,
        summarize=_unused,
        report=_unused,
        validate_launch=_unused,
        reduce_transition=_unused,
    )
    workflow = Workflow(
        WorkflowSpec(
            workflow_id="research",
            version="1.0.0",
            title="Research",
            phases=(ConversationPhase("creation"), TerminalPhase("done")),
            transitions={"creation": frozenset({"done"}), "done": frozenset()},
        ),
        doctrine=doctrine,
        ports=WorkflowPorts(
            chat=chat,
            clock=lambda: NOW,
            id_factory=lambda prefix: f"{prefix}-test",
            conversations=conversations,
            execution_store=InMemoryExecutionStore(),
        ),
    )
    return workflow, chat, conversations, execution


def test_body_steering_delivers_once_and_immediate_worker_sees_committed_epoch(
    runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, chat, conversations, initial = runtime
    writer = Mock(wraps=conversations.deliver_session_message)
    create = Mock(wraps=chat.create_message)
    schedule_input = Mock(wraps=conversations.schedule_input)
    monkeypatch.setattr(conversations, "deliver_session_message", writer)
    monkeypatch.setattr(chat, "create_message", create)
    monkeypatch.setattr(conversations, "schedule_input", schedule_input)
    observed: list[ChatMessage] = []

    def immediate_worker(
        workspace_id, chat_id, schedule_id, *, input_id, input_position
    ):
        current = chat.get_execution(workspace_id, initial.execution_id)
        message = chat.get_message(workspace_id, input_id)
        assert current.intent_epoch == initial.intent_epoch + 1
        assert current.steering_message_ids == (message.message_id,)
        assert schedule_id == ""
        assert chat_id == initial.chat_id
        assert input_position == message.input_position == 2
        assert message.execution_id == current.execution_id
        assert message.intent_epoch == current.intent_epoch
        assert message.actor_id == "operator"
        assert message.role is MessageRole.USER
        assert decode_activity(message).sub_agent_id is None
        assert message.data is not None and "sessionMessage" in message.data
        canonical = from_record(message)
        assert canonical.direction == "incoming"
        assert canonical.content.source == "user"
        assert canonical.content.text == "Use a shorter horizon"
        assert canonical.position == input_position
        assert canonical.content.correlation is None
        native = project_native_messages((canonical,))
        assert native[0].role == "user"
        assert json.loads(native[0].content) == {
            "text": "Use a shorter horizon",
            "source": "user",
            "result": None,
            "correlation": None,
        }
        events = chat.replay_events(workspace_id, current.execution_id).events
        steered = [
            event
            for event in events
            if event.event_type is ExecutionEventType.WORKFLOW_STEERED
        ]
        assert len(steered) == 1
        assert steered[0].correlation_id == message.message_id
        assert steered[0].payload["intentEpoch"] == current.intent_epoch
        observed.append(message)

    worker = Mock(side_effect=immediate_worker)
    monkeypatch.setattr(conversations._workers, "schedule", worker)
    result = workflow.steer(
        "workspace",
        initial.execution_id,
        actor_id="operator",
        body="Use a shorter horizon",
    )
    assert result == chat.get_execution("workspace", initial.execution_id)
    assert result.steering_message_ids == (observed[0].message_id,)
    assert (
        writer.call_count
        == create.call_count
        == schedule_input.call_count
        == worker.call_count
        == 1
    )
    assert writer.call_args.kwargs["schedule"] is False
    assert len(chat.list_messages("workspace", initial.chat_id)) == 2


@pytest.mark.parametrize("canonical", [True, False])
def test_existing_message_is_not_redelivered_or_scheduled(
    runtime, monkeypatch, canonical
) -> None:
    workflow, chat, conversations, execution = runtime
    if canonical:
        message = conversations.root("workspace", execution.chat_id).send(
            "Existing input",
            actor_id="operator",
            execution_id=execution.execution_id,
            schedule=False,
        )
    else:
        message = chat.create_message(
            "workspace",
            execution.chat_id,
            actor_id="operator",
            body="Legacy accepted input",
            execution_id=execution.execution_id,
        )
    writer = Mock(
        side_effect=AssertionError("existing input must not be delivered again")
    )
    scheduler = Mock(
        side_effect=AssertionError("existing input must not be scheduled again")
    )
    monkeypatch.setattr(conversations, "deliver_session_message", writer)
    monkeypatch.setattr(conversations, "schedule_input", scheduler)
    result = workflow.steer(
        "workspace",
        execution.execution_id,
        actor_id="operator",
        body="Not a new message",
        message_id=message.message_id,
    )
    assert result.intent_epoch == execution.intent_epoch + 1
    assert result.steering_message_ids == (message.message_id,)
    assert chat.get_message("workspace", message.message_id) == message
    assert len(chat.list_messages("workspace", execution.chat_id)) == 2
    writer.assert_not_called()
    scheduler.assert_not_called()


def test_stopped_execution_does_not_deliver_or_wake(runtime, monkeypatch) -> None:
    workflow, chat, conversations, execution = runtime
    chat.stop_workflow("workspace", execution.execution_id, actor_id="operator")
    writer = Mock(side_effect=AssertionError("rejected steering must not deliver"))
    scheduler = Mock(side_effect=AssertionError("rejected steering must not wake"))
    monkeypatch.setattr(conversations, "deliver_session_message", writer)
    monkeypatch.setattr(conversations, "schedule_input", scheduler)
    with pytest.raises(InvalidLifecycleTransition):
        workflow.steer(
            "workspace", execution.execution_id, actor_id="operator", body="Too late"
        )
    assert len(chat.list_messages("workspace", execution.chat_id)) == 1
    writer.assert_not_called()
    scheduler.assert_not_called()


def test_unknown_execution_does_not_deliver_or_wake(runtime, monkeypatch) -> None:
    workflow, chat, conversations, execution = runtime
    scheduler = Mock(side_effect=AssertionError("unknown execution must not wake"))
    monkeypatch.setattr(conversations, "schedule_input", scheduler)
    with pytest.raises(RecordNotFound):
        workflow.steer("workspace", "missing", actor_id="operator", body="No target")
    assert len(chat.list_messages("workspace", execution.chat_id)) == 1
    scheduler.assert_not_called()


def test_delivery_failure_does_not_advance_epoch_or_wake(runtime, monkeypatch) -> None:
    workflow, chat, conversations, execution = runtime
    writer = Mock(side_effect=OSError("persistence failed"))
    scheduler = Mock(side_effect=AssertionError("failed delivery must not wake"))
    monkeypatch.setattr(conversations, "deliver_session_message", writer)
    monkeypatch.setattr(conversations, "schedule_input", scheduler)
    with pytest.raises(OSError, match="persistence failed"):
        workflow.steer(
            "workspace", execution.execution_id, actor_id="operator", body="Not durable"
        )
    assert chat.get_execution("workspace", execution.execution_id) == execution
    assert len(chat.list_messages("workspace", execution.chat_id)) == 1
    writer.assert_called_once()
    scheduler.assert_not_called()
