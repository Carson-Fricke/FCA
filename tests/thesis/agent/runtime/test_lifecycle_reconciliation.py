"""Startup reconciliation of durable sub-agent records stranded by a restart."""

from __future__ import annotations

from thesis.agent.runtime.lifecycle import (
    SubAgentLifecycle,
    reconcile_stranded_sub_agents,
)
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat.contracts import (
    WORKFLOW_NODE_ROLE,
    ChatThread,
    SubAgentStatus,
    WorkflowDefinition,
    WorkflowExecutionState,
    replace_record,
)
from thesis.chat.service import ChatService, InMemoryChatPersistence

_WORKSPACE = "ws-reconcile"
_ACTOR = "operator-1"
_WORKFLOW = WorkflowDefinition(
    workflow_id="strategy-development",
    version="1.0.0",
    title="Strategy development",
    phases=("understand", "implement"),
)


def _service() -> ChatService:
    return ChatService(persistence=InMemoryChatPersistence())


def _lifecycle(chat: ChatService) -> SubAgentLifecycle:
    return SubAgentLifecycle(chat, store=PersistenceSubAgentStore(chat.persistence))


def _chat(chat: ChatService, *, workspace: str = _WORKSPACE) -> str:
    return chat.create_chat(workspace, title="Test chat", owner_actor_id=_ACTOR).chat_id


def _execution(chat: ChatService, chat_id: str, *, workspace: str = _WORKSPACE) -> str:
    execution = chat.launch_workflow(
        workspace,
        chat_id,
        workflow=_WORKFLOW,
        actor_id=_ACTOR,
        body="Build and test the strategy.",
    )
    return execution.execution_id


def _spawn(
    chat: ChatService,
    chat_id: str,
    *,
    workflow_execution_id: str | None,
    parent_sub_agent_id: str | None = None,
    role: str = WORKFLOW_NODE_ROLE,
    workspace: str = _WORKSPACE,
) -> str:
    record = _lifecycle(chat).spawn(
        workspace,
        chat_id,
        spec=SubAgentSpec(role=role),
        actor_id=_ACTOR,
        parent_sub_agent_id=parent_sub_agent_id,
        workflow_execution_id=workflow_execution_id,
    )
    return record.sub_agent_id


def _status(chat: ChatService, sub_agent_id: str) -> SubAgentStatus:
    record = PersistenceSubAgentStore(chat.persistence).get_sub_agent(
        _WORKSPACE, sub_agent_id
    )
    assert record is not None
    return record.status


def test_reconcile_cancels_a_stopped_executions_node_and_descendants() -> None:
    chat = _service()
    chat_id = _chat(chat)
    execution_id = _execution(chat, chat_id)
    node = _spawn(chat, chat_id, workflow_execution_id=execution_id)
    phase = _spawn(
        chat,
        chat_id,
        workflow_execution_id=execution_id,
        parent_sub_agent_id=node,
        role="strategy-development/understand",
    )
    chat.stop_active_executions(_WORKSPACE)

    cancelled = reconcile_stranded_sub_agents(chat, _WORKSPACE)

    assert set(cancelled) == {node, phase}
    assert _status(chat, node) is SubAgentStatus.CANCELLED
    assert _status(chat, phase) is SubAgentStatus.CANCELLED


def test_reconcile_cancels_a_paused_executions_node() -> None:
    chat = _service()
    chat_id = _chat(chat)
    execution_id = _execution(chat, chat_id)
    node = _spawn(chat, chat_id, workflow_execution_id=execution_id)
    # A paused row can only arrive from history: nothing writes the state
    # anymore. A restart leaves no worker able to continue it, so its node is
    # stranded exactly like a stopped one's.
    execution = chat.get_execution(_WORKSPACE, execution_id)
    chat.persistence.save(
        replace_record(execution, state=WorkflowExecutionState.PAUSED)
    )

    cancelled = reconcile_stranded_sub_agents(chat, _WORKSPACE)

    assert cancelled == (node,)
    assert _status(chat, node) is SubAgentStatus.CANCELLED


def test_reconcile_cancels_a_node_whose_execution_is_absent() -> None:
    chat = _service()
    chat_id = _chat(chat)
    node = _spawn(chat, chat_id, workflow_execution_id="execution-missing")

    cancelled = reconcile_stranded_sub_agents(chat, _WORKSPACE)

    assert cancelled == (node,)
    assert _status(chat, node) is SubAgentStatus.CANCELLED


def test_reconcile_leaves_a_running_executions_records_alone() -> None:
    chat = _service()
    chat_id = _chat(chat)
    execution_id = _execution(chat, chat_id)
    node = _spawn(chat, chat_id, workflow_execution_id=execution_id)
    lifecycle = _lifecycle(chat)
    lifecycle.start(_WORKSPACE, node)
    phase = _spawn(
        chat,
        chat_id,
        workflow_execution_id=execution_id,
        parent_sub_agent_id=node,
        role="strategy-development/understand",
    )

    cancelled = reconcile_stranded_sub_agents(chat, _WORKSPACE)

    assert cancelled == ()
    assert _status(chat, node) is SubAgentStatus.RUNNING
    assert _status(chat, phase) is SubAgentStatus.PENDING


def test_reconcile_cancels_records_whose_chat_no_longer_exists() -> None:
    chat = _service()
    chat_id = _chat(chat)
    node = _spawn(chat, chat_id, workflow_execution_id=None)
    child = _spawn(
        chat,
        chat_id,
        workflow_execution_id=None,
        parent_sub_agent_id=node,
        role="research",
    )
    # Simulate a conversation removed without cascading to its sub-agents.
    chat.persistence.delete(ChatThread, _WORKSPACE, chat_id)

    cancelled = reconcile_stranded_sub_agents(chat, _WORKSPACE)

    assert set(cancelled) == {node, child}
    assert _status(chat, node) is SubAgentStatus.CANCELLED
    assert _status(chat, child) is SubAgentStatus.CANCELLED


def test_reconcile_preserves_a_terminal_descendant() -> None:
    chat = _service()
    chat_id = _chat(chat)
    execution_id = _execution(chat, chat_id)
    node = _spawn(chat, chat_id, workflow_execution_id=execution_id)
    child = _spawn(
        chat,
        chat_id,
        workflow_execution_id=execution_id,
        parent_sub_agent_id=node,
        role="strategy-development/understand",
    )
    lifecycle = _lifecycle(chat)
    lifecycle.complete(_WORKSPACE, child, result={"verdict": "supported"})
    chat.stop_active_executions(_WORKSPACE)

    cancelled = reconcile_stranded_sub_agents(chat, _WORKSPACE)

    assert cancelled == (node,)
    assert _status(chat, node) is SubAgentStatus.CANCELLED
    assert _status(chat, child) is SubAgentStatus.COMPLETED


def test_reconcile_is_idempotent_and_scoped_to_one_workspace() -> None:
    chat = _service()
    chat_id = _chat(chat)
    node = _spawn(chat, chat_id, workflow_execution_id="execution-missing")
    other_chat_id = _chat(chat, workspace="ws-other")
    other = _spawn(
        chat,
        other_chat_id,
        workflow_execution_id="execution-other",
        workspace="ws-other",
    )

    first = reconcile_stranded_sub_agents(chat, _WORKSPACE)
    stored = PersistenceSubAgentStore(chat.persistence).get_sub_agent(_WORKSPACE, node)
    second = reconcile_stranded_sub_agents(chat, _WORKSPACE)

    assert first == (node,)
    assert second == ()
    # A record that is already terminal is never rewritten by a rerun.
    assert (
        PersistenceSubAgentStore(chat.persistence).get_sub_agent(_WORKSPACE, node)
        == stored
    )
    other_record = PersistenceSubAgentStore(chat.persistence).get_sub_agent(
        "ws-other", other
    )
    assert other_record is not None
    assert other_record.status is SubAgentStatus.PENDING
