from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat import (
    ChatService,
    FileChatPersistence,
    MessageRole,
    RecordNotFound,
    SubAgentStatus,
    TranscriptItemKind,
    WorkflowDefinition,
)
from thesis.chat.contracts import ChatMessage, ChatThread, SubAgentKind, SubAgentRecord
from thesis.execution.events import ExecutionEventType

NOW = datetime(2026, 1, 1, tzinfo=UTC)
WORKFLOW = WorkflowDefinition(
    workflow_id="hypothesis-development",
    version="1.0.0",
    title="Hypothesis development",
    phases=("requested", "ideation", "verdict"),
)


def _service(root: Path) -> ChatService:
    counters: dict[str, int] = {}

    def identifier(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]}"

    return ChatService(
        persistence=FileChatPersistence(root), clock=lambda: NOW, id_factory=identifier
    )


def test_records_survive_a_new_service_over_the_same_root(tmp_path: Path) -> None:
    service = _service(tmp_path)
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    message = service.create_message(
        "workspace-a",
        chat.chat_id,
        actor_id="thesis-agent",
        body="The signal survives costs.",
        role=MessageRole.ASSISTANT,
        reasoning="weigh fee drag first",
    )
    anchor = service.create_message(
        "workspace-a",
        chat.chat_id,
        actor_id="operator",
        body="Hypothesis development",
        role=MessageRole.SYSTEM,
        kind=TranscriptItemKind.WORKFLOW,
        data={"executionId": "execution-1", "nested": {"thresholds": [1, 2]}},
    )
    tool_message = service.create_message(
        "workspace-a",
        chat.chat_id,
        actor_id="thesis-agent",
        body="run_backtest complete",
        role=MessageRole.TOOL,
        tool_result={
            "tool": "run_backtest",
            "callId": "call-1",
            "ok": True,
            "summary": "done",
            "structured": {"run_id": "run-1"},
            "text": "",
            "failure": None,
        },
        kind=TranscriptItemKind.TOOL,
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
    investigation = service.create_investigation(
        "workspace-a",
        title="Validate momentum",
        objective="Does the signal survive costs?",
        owner_actor_id="operator",
        chat_ids=(chat.chat_id,),
    )
    execution = service.launch_workflow(
        "workspace-a",
        chat.chat_id,
        workflow=WORKFLOW,
        actor_id="operator",
        body="Research the idea.",
    )
    job = service.submit_tool_job(
        "workspace-a",
        execution.execution_id,
        tool_name="run_backtest",
        actor_id="operator",
    )

    restarted = _service(tmp_path)
    assert restarted.get_chat("workspace-a", chat.chat_id).title == "Research"
    stored_message = restarted.get_message("workspace-a", message.message_id)
    assert stored_message.reasoning == "weigh fee drag first"
    stored_anchor = restarted.get_message("workspace-a", anchor.message_id)
    assert stored_anchor.kind is TranscriptItemKind.WORKFLOW
    assert stored_anchor.data == {
        "executionId": "execution-1",
        "nested": {"thresholds": [1, 2]},
    }
    stored_tool = restarted.get_message("workspace-a", tool_message.message_id)
    assert stored_tool.tool_result is not None
    assert stored_tool.tool_result["structured"] == {"run_id": "run-1"}
    assert (
        restarted.get_attachment("workspace-a", attachment.attachment_id).filename
        == "notes.txt"
    )
    assert (
        restarted.get_investigation(
            "workspace-a", investigation.investigation_id
        ).objective
        == "Does the signal survive costs?"
    )
    assert (
        restarted.get_execution("workspace-a", execution.execution_id).workflow_id
        == "hypothesis-development"
    )
    assert (
        restarted.get_tool_job("workspace-a", job.tool_job_id).tool_name
        == "run_backtest"
    )
    stored_messages = restarted.list_messages("workspace-a", chat.chat_id)
    assert [item.message_id for item in stored_messages][:3] == [
        message.message_id,
        anchor.message_id,
        tool_message.message_id,
    ]


def test_delete_removes_the_durable_document(tmp_path: Path) -> None:
    service = _service(tmp_path)
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    service.create_message(
        "workspace-a", chat.chat_id, actor_id="operator", body="Hello"
    )

    service.delete_chat("workspace-a", chat.chat_id)
    restarted = _service(tmp_path)
    assert restarted.list_chats("workspace-a") == ()
    with pytest.raises(RecordNotFound):
        restarted.list_messages("workspace-a", chat.chat_id)


def test_delete_cancels_active_workflow_before_cascading(tmp_path: Path) -> None:
    service = _service(tmp_path)
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    execution = service.launch_workflow(
        "workspace-a",
        chat.chat_id,
        workflow=WorkflowDefinition(
            workflow_id="hypothesis-development",
            version="1.0.0",
            title="Hypothesis development",
            phases=("requested", "hypothesis_creation"),
        ),
        actor_id="operator",
        body="Explore the idea.",
    )
    assert execution.state.value == "running"

    service.delete_chat("workspace-a", chat.chat_id)

    event_types = [
        event.event_type
        for event in service.event_store.replay(execution.execution_id).events
    ]
    assert ExecutionEventType.WORKFLOW_CANCELLED in event_types
    with pytest.raises(RecordNotFound):
        service.get_execution("workspace-a", execution.execution_id)


def test_delete_cancels_the_chats_sub_agents_and_spares_other_chats(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    first = service.create_chat("workspace-a", title="First", owner_actor_id="operator")
    second = service.create_chat(
        "workspace-a", title="Second", owner_actor_id="operator"
    )
    store = PersistenceSubAgentStore(service.persistence)
    lifecycle = SubAgentLifecycle(service, store=store)
    node = lifecycle.spawn(
        "workspace-a",
        first.chat_id,
        spec=SubAgentSpec(role="research"),
        actor_id="operator",
    )
    child = lifecycle.spawn(
        "workspace-a",
        first.chat_id,
        spec=SubAgentSpec(role="research"),
        actor_id="operator",
        parent_sub_agent_id=node.sub_agent_id,
    )
    other = lifecycle.spawn(
        "workspace-a",
        second.chat_id,
        spec=SubAgentSpec(role="research"),
        actor_id="operator",
    )

    service.delete_chat("workspace-a", first.chat_id)

    stored_node = store.get_sub_agent("workspace-a", node.sub_agent_id)
    stored_child = store.get_sub_agent("workspace-a", child.sub_agent_id)
    stored_other = store.get_sub_agent("workspace-a", other.sub_agent_id)
    # Cancelled, not deleted: the durable status is the cancellation fence a
    # background node turn reads, so it must survive the chat removal.
    assert stored_node is not None and stored_node.status is SubAgentStatus.CANCELLED
    assert stored_child is not None and stored_child.status is SubAgentStatus.CANCELLED
    assert stored_other is not None and stored_other.status is SubAgentStatus.PENDING


def test_independent_chat_workflows_start_without_a_shared_queue(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    first = service.create_chat("workspace-a", title="First", owner_actor_id="operator")
    second = service.create_chat(
        "workspace-a", title="Second", owner_actor_id="operator"
    )

    first_execution = service.launch_workflow(
        "workspace-a", first.chat_id, workflow=WORKFLOW, actor_id="operator", body="one"
    )
    second_execution = service.launch_workflow(
        "workspace-a",
        second.chat_id,
        workflow=WORKFLOW,
        actor_id="operator",
        body="two",
    )

    assert first_execution.state.value == "running"
    assert second_execution.state.value == "running"


def test_records_are_isolated_by_workspace(tmp_path: Path) -> None:
    store = FileChatPersistence(tmp_path)
    alpha = store.save(
        ChatThread(
            workspace_id="workspace-a",
            created_at=NOW,
            created_by="operator",
            chat_id="chat-a",
            title="Alpha",
            owner_actor_id="operator",
        )
    )
    store.save(
        ChatThread(
            workspace_id="workspace-b",
            created_at=NOW,
            created_by="operator",
            chat_id="chat-b",
            title="Bravo",
            owner_actor_id="operator",
        )
    )

    assert [chat.chat_id for chat in store.list(ChatThread, "workspace-a")] == [
        "chat-a"
    ]
    assert store.get(ChatThread, "workspace-b", "chat-a") is None
    assert store.get(ChatThread, "workspace-a", alpha.chat_id) is not None


def test_tool_result_defaults_survive_round_trip(tmp_path: Path) -> None:
    store = FileChatPersistence(tmp_path)
    message = ChatMessage(
        workspace_id="workspace-a",
        created_at=NOW,
        created_by="operator",
        message_id="message-1",
        chat_id="chat-a",
        actor_id="operator",
        role=MessageRole.USER,
        body="Hello",
    )
    store.save(message)

    loaded = store.get(ChatMessage, "workspace-a", "message-1")
    assert loaded is not None
    assert loaded.tool_result is None
    assert loaded.data is None
    assert loaded.kind is TranscriptItemKind.MESSAGE


def test_sub_agent_phase_survives_a_persistence_round_trip(tmp_path: Path) -> None:
    service = _service(tmp_path)
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    store = PersistenceSubAgentStore(service.persistence)
    lifecycle = SubAgentLifecycle(service, store=store)
    child = lifecycle.spawn(
        "workspace-a",
        chat.chat_id,
        spec=SubAgentSpec(
            role="hypothesis-creation",
            kind=SubAgentKind.CONVERSATION,
            phase="hypothesis_creation",
        ),
        actor_id="operator",
        workflow_execution_id="execution-1",
    )

    reloaded = _service(tmp_path).persistence.get(
        SubAgentRecord, "workspace-a", child.sub_agent_id
    )

    assert reloaded is not None
    assert reloaded.phase == "hypothesis_creation"
