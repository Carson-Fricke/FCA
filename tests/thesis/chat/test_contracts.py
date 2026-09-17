from __future__ import annotations

from datetime import UTC, datetime

import pytest

from thesis.chat import (
    AttachmentState,
    ChatService,
    CrossWorkspaceLinkError,
    InMemoryChatPersistence,
    InvalidLifecycleTransition,
    MessageRole,
    MessageState,
    ToolJobState,
    TranscriptItemKind,
    WorkflowDefinition,
    WorkflowExecutionState,
)
from thesis.chat.contracts import (
    WORKFLOW_NODE_ROLE,
    SubAgentKind,
    SubAgentRecord,
)
from thesis.execution.events import ActorScope, ExecutionEventType

NOW = datetime(2026, 1, 1, tzinfo=UTC)
WORKFLOW = WorkflowDefinition(
    workflow_id="strategy-development",
    version="1.0.0",
    title="Strategy development",
    phases=("understand", "implement", "evaluate"),
)


def _service() -> ChatService:
    counters: dict[str, int] = {}

    def identifier(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]}"

    return ChatService(clock=lambda: NOW, id_factory=identifier)


def test_multiple_records_are_workspace_owned_and_explicitly_linked() -> None:
    service = _service()
    first_chat = service.create_chat(
        "workspace-a", title="Momentum", owner_actor_id="operator"
    )
    second_chat = service.create_chat(
        "workspace-a", title="Mean reversion", owner_actor_id="operator"
    )
    investigation = service.create_investigation(
        "workspace-a",
        title="Validate momentum",
        objective="Determine whether the signal survives costs.",
        owner_actor_id="operator",
        chat_ids=(first_chat.chat_id,),
        strategy_ids=("strategy-momentum",),
        experiment_ids=("experiment-costs",),
    )

    assert len(service.list_chats("workspace-a")) == 2
    assert [
        item.investigation_id for item in service.list_investigations("workspace-a")
    ] == [investigation.investigation_id]
    assert investigation.chat_ids == (first_chat.chat_id,)
    assert service.get_chat("workspace-a", first_chat.chat_id).investigation_ids == (
        investigation.investigation_id,
    )
    assert service.get_chat("workspace-a", second_chat.chat_id).investigation_ids == ()
    with pytest.raises((CrossWorkspaceLinkError, LookupError)):
        service.create_chat(
            "workspace-b",
            title="Cross-boundary",
            owner_actor_id="operator",
            investigation_ids=(investigation.investigation_id,),
        )


def test_message_and_attachment_lifecycle_commands_are_explicit() -> None:
    service = _service()
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    message = service.create_message(
        "workspace-a", chat.chat_id, actor_id="operator", body="Upload this evidence."
    )
    attachment = service.create_attachment(
        "workspace-a",
        chat.chat_id,
        message.message_id,
        actor_id="operator",
        filename="notes.txt",
        media_type="text/plain",
        size_bytes=12,
    )

    service.transition_attachment(
        "workspace-a", attachment.attachment_id, AttachmentState.UPLOADING
    )
    service.upload_attachment(
        "workspace-a",
        attachment.attachment_id,
        content_uri="artifact://notes.txt",
        sha256="a" * 64,
    )
    available = service.scan_attachment(
        "workspace-a", attachment.attachment_id, available=True
    )
    assert available.state is AttachmentState.AVAILABLE

    service.start_message("workspace-a", message.message_id)
    completed = service.complete_message("workspace-a", message.message_id)
    assert completed.state is MessageState.COMPLETED
    with pytest.raises(InvalidLifecycleTransition):
        service.start_message("workspace-a", message.message_id)


def test_message_reasoning_round_trips_and_serializes_without_touching_body() -> None:
    store = InMemoryChatPersistence()
    service = ChatService(persistence=store, clock=lambda: NOW)
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    message = service.create_message(
        "workspace-a",
        chat.chat_id,
        actor_id="thesis-agent",
        body="The signal survives costs.",
        role=MessageRole.ASSISTANT,
        reasoning="First weigh the fee drag, then the turnover.",
    )

    assert message.reasoning == "First weigh the fee drag, then the turnover."
    assert "First weigh" not in message.body
    portable = message.as_dict()
    assert portable["body"] == "The signal survives costs."
    assert portable["reasoning"] == "First weigh the fee drag, then the turnover."

    reloaded = ChatService(persistence=store, clock=lambda: NOW)
    stored = reloaded.get_message("workspace-a", message.message_id)
    assert stored.reasoning == message.reasoning
    assert stored.body == message.body


def test_message_reasoning_defaults_to_none_and_collapses_blank() -> None:
    service = _service()
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    plain = service.create_message(
        "workspace-a", chat.chat_id, actor_id="operator", body="Hi"
    )
    blank = service.create_message(
        "workspace-a",
        chat.chat_id,
        actor_id="operator",
        body="Hi again",
        reasoning="   ",
    )

    assert plain.reasoning is None
    assert blank.reasoning is None
    assert plain.as_dict()["reasoning"] is None


def test_workflow_commands_emit_ordered_replayable_events_with_actor_scope() -> None:
    service = _service()
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    execution = service.launch_workflow(
        "workspace-a",
        chat.chat_id,
        workflow=WORKFLOW,
        actor_id="operator",
        body="Build and test the strategy.",
    )
    service.start_workflow("workspace-a", execution.execution_id, actor_id="worker")
    steered = service.steer_workflow(
        "workspace-a",
        execution.execution_id,
        actor_id="operator",
        body="Use lower fees.",
    )
    replay = service.replay_events("workspace-a", execution.execution_id)
    cursors = [event.cursor for event in replay]

    assert steered.intent_epoch == 1
    assert cursors == list(range(1, len(cursors) + 1))
    assert replay.next_cursor == cursors[-1]
    assert replay.events[0].event_type is ExecutionEventType.MESSAGE_ACCEPTED
    assert replay.events[0].actor_scope is ActorScope.USER
    assert replay.events[1].event_type is ExecutionEventType.WORKFLOW_LAUNCHED
    assert replay.events[2].event_type is ExecutionEventType.WORKFLOW_STARTED
    assert replay.events[-1].intent_epoch == 1

    reconnect = service.replay_events(
        "workspace-a", execution.execution_id, after_cursor=replay.events[0].cursor
    )
    assert reconnect.events[0].cursor == replay.events[0].cursor + 1
    assert (
        service.replay_events(
            "workspace-a", execution.execution_id, after_cursor=replay.next_cursor
        ).events
        == ()
    )


def test_late_tool_results_are_retained_and_marked_after_cancel() -> None:
    service = _service()
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    execution = service.launch_workflow(
        "workspace-a",
        chat.chat_id,
        workflow=WORKFLOW,
        actor_id="operator",
        body="Run the experiment.",
    )
    job = service.submit_tool_job(
        "workspace-a",
        execution.execution_id,
        tool_name="backtest",
        actor_id="worker",
        correlation_id="backtest-correlation",
    )
    service.cancel_workflow("workspace-a", execution.execution_id, actor_id="operator")
    result = service.record_tool_result(
        "workspace-a",
        job.tool_job_id,
        result={"sharpe": 1.2},
        actor_id="backtest",
    )
    events = service.replay_events("workspace-a", execution.execution_id).events

    assert result.state is ToolJobState.SUCCEEDED
    assert result.result == {"sharpe": 1.2}
    assert result.late_result is True
    assert events[-1].event_type is ExecutionEventType.TOOL_RESULT
    assert events[-1].late_result is True
    assert events[-1].correlation_id == "backtest-correlation"


def test_transcript_item_kind_and_data_round_trip() -> None:
    store = InMemoryChatPersistence()
    service = ChatService(persistence=store, clock=lambda: NOW)
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    plain = service.create_message(
        "workspace-a", chat.chat_id, actor_id="operator", body="Hello"
    )
    anchor = service.create_message(
        "workspace-a",
        chat.chat_id,
        actor_id="operator",
        body="Hypothesis development",
        role=MessageRole.SYSTEM,
        kind=TranscriptItemKind.WORKFLOW,
        data={
            "executionId": "execution-1",
            "phase": "requested",
            "nested": {"thresholds": [1, 2]},
        },
    )

    assert plain.kind is TranscriptItemKind.MESSAGE
    assert plain.as_dict()["kind"] == "message"
    assert plain.as_dict()["data"] is None

    assert anchor.kind is TranscriptItemKind.WORKFLOW
    portable = anchor.as_dict()
    assert portable["kind"] == "workflow"
    assert portable["data"] == {
        "executionId": "execution-1",
        "phase": "requested",
        "nested": {"thresholds": [1, 2]},
    }

    reloaded = ChatService(persistence=store, clock=lambda: NOW)
    stored = reloaded.get_message("workspace-a", anchor.message_id)
    assert stored.kind is TranscriptItemKind.WORKFLOW
    assert stored.data == {
        "executionId": "execution-1",
        "phase": "requested",
        "nested": {"thresholds": [1, 2]},
    }


def test_event_store_limit_returns_cursor_for_next_reconnect_page() -> None:
    service = _service()
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    execution = service.launch_workflow(
        "workspace-a",
        chat.chat_id,
        workflow=WORKFLOW,
        actor_id="operator",
        body="Run it.",
    )
    service.start_workflow("workspace-a", execution.execution_id, actor_id="worker")

    first = service.replay_events("workspace-a", execution.execution_id, limit=1)
    second = service.replay_events(
        "workspace-a", execution.execution_id, after_cursor=first.next_cursor, limit=1
    )

    assert first.has_more is True
    assert second.events[0].cursor == first.next_cursor + 1


def _workflow_node(
    service: ChatService,
    chat_id: str,
    *,
    sub_agent_id: str,
    execution_id: str,
    parent_sub_agent_id: str | None,
) -> SubAgentRecord:
    record = SubAgentRecord(
        workspace_id="workspace-a",
        created_at=NOW,
        created_by="operator",
        sub_agent_id=sub_agent_id,
        chat_id=chat_id,
        role=WORKFLOW_NODE_ROLE,
        kind=SubAgentKind.ONE_SHOT,
        parent_sub_agent_id=parent_sub_agent_id,
        workflow_execution_id=execution_id,
        updated_at=NOW,
    )
    return service.persistence.save(record)


def test_node_launched_executions_are_owned_by_their_node() -> None:
    service = _service()
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    root = service.launch_workflow(
        "workspace-a",
        chat.chat_id,
        workflow=WORKFLOW,
        actor_id="operator",
        body="Root idea.",
    )
    anchored = service.launch_workflow(
        "workspace-a",
        chat.chat_id,
        workflow=WORKFLOW,
        actor_id="operator",
        body="Node idea.",
    )
    _workflow_node(
        service,
        chat.chat_id,
        sub_agent_id="subagent-node",
        execution_id=anchored.execution_id,
        parent_sub_agent_id="subagent-anchor",
    )

    assert service.list_executions("workspace-a") == (root,)
    assert service.list_node_executions("workspace-a", "subagent-anchor") == (anchored,)
    assert service.list_node_executions("workspace-a", "subagent-other") == ()
    assert service.execution_anchor("workspace-a", anchored.execution_id) == (
        "subagent-anchor"
    )
    assert service.execution_anchor("workspace-a", root.execution_id) is None


def test_stop_active_executions_still_covers_node_launched_executions() -> None:
    service = _service()
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    root = service.launch_workflow(
        "workspace-a",
        chat.chat_id,
        workflow=WORKFLOW,
        actor_id="operator",
        body="Root idea.",
    )
    anchored = service.launch_workflow(
        "workspace-a",
        chat.chat_id,
        workflow=WORKFLOW,
        actor_id="operator",
        body="Node idea.",
    )
    _workflow_node(
        service,
        chat.chat_id,
        sub_agent_id="subagent-node",
        execution_id=anchored.execution_id,
        parent_sub_agent_id="subagent-anchor",
    )

    stopped = service.stop_active_executions("workspace-a")

    assert {execution.execution_id for execution in stopped} == {
        root.execution_id,
        anchored.execution_id,
    }
    assert (
        service.get_execution("workspace-a", anchored.execution_id).state
        is WorkflowExecutionState.STOPPED
    )
