"""Unit tests for the sub-agent event contract and event-log projection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from thesis.execution.events import (
    ActorScope,
    ExecutionEvent,
    ExecutionEventType,
    InMemoryExecutionEventStore,
)
from thesis.execution.sub_agents import (
    STATUS_NEEDS_INPUT,
    SubAgentContextSpec,
    SubAgentContractError,
    SubAgentTree,
    append_gate_event,
    append_sub_agent_event,
    filter_replay,
    sub_agent_payload,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
EXECUTION = "execution-1"
WORKSPACE = "workspace-a"


def _spawn(
    store: InMemoryExecutionEventStore,
    sub_agent_id: str,
    parent: str | None,
) -> ExecutionEvent:
    return append_sub_agent_event(
        store,
        execution_id=EXECUTION,
        workspace_id=WORKSPACE,
        event_type=ExecutionEventType.SUB_AGENT_SPAWNED,
        occurred_at=NOW,
        sub_agent_id=sub_agent_id,
        parent_sub_agent_id=parent,
        phase="ideation",
        role="orchestrator",
        context=SubAgentContextSpec(),
    )


def test_context_spec_is_isolated_and_normalizes_cutoff() -> None:
    spec = SubAgentContextSpec(
        knowledge_cutoff=NOW, mcp_enabled=True, mcp_tools=("a", "a", "b")
    )

    assert spec.as_dict() == {
        "isolated": True,
        "knowledge_cutoff": "2026-01-01T00:00:00Z",
        "mcp": {"enabled": True, "tools": ["a", "b"]},
    }


def test_context_spec_rejects_a_naive_cutoff() -> None:
    with pytest.raises(SubAgentContractError):
        SubAgentContextSpec(knowledge_cutoff=datetime(2026, 1, 1))  # noqa: DTZ001


def test_sub_agent_payload_carries_the_full_contract() -> None:
    payload = sub_agent_payload(
        sub_agent_id="a",
        parent_sub_agent_id=None,
        phase="ideation",
        role="orchestrator",
        context=SubAgentContextSpec(),
    )

    assert set(payload) == {
        "acceptsInput",
        "context",
        "parentSubAgentId",
        "phase",
        "role",
        "subAgentId",
    }
    assert payload["acceptsInput"] is False


def _append(
    store: InMemoryExecutionEventStore,
    event_type: ExecutionEventType,
    *,
    extra: dict[str, object] | None = None,
) -> ExecutionEvent:
    return append_sub_agent_event(
        store,
        execution_id=EXECUTION,
        workspace_id=WORKSPACE,
        event_type=event_type,
        occurred_at=NOW,
        sub_agent_id="a",
        parent_sub_agent_id=None,
        phase="ideation",
        role="orchestrator",
        context=SubAgentContextSpec(),
        extra=extra,
    )


def test_append_sub_agent_event_rejects_invalid_nested_payloads() -> None:
    store = InMemoryExecutionEventStore()

    with pytest.raises(SubAgentContractError):
        _append(store, ExecutionEventType.WORKFLOW_PHASE)
    with pytest.raises(SubAgentContractError):
        _append(
            store,
            ExecutionEventType.SUB_AGENT_PROGRESS,
            extra={"text": "missing kind"},
        )
    with pytest.raises(SubAgentContractError):
        _append(
            store,
            ExecutionEventType.SUB_AGENT_PROGRESS,
            extra={"kind": "thinking"},
        )
    with pytest.raises(SubAgentContractError):
        _append(
            store,
            ExecutionEventType.SUB_AGENT_OUTPUT,
            extra={"kind": "artifact"},
        )
    with pytest.raises(SubAgentContractError):
        _append(store, ExecutionEventType.SUB_AGENT_NEEDS_INPUT, extra={})
    with pytest.raises(SubAgentContractError):
        _append(
            store,
            ExecutionEventType.SUB_AGENT_COMPLETED,
            extra={"status": "completed"},
        )


def test_append_gate_event_rejects_non_gate_types() -> None:
    store = InMemoryExecutionEventStore()
    with pytest.raises(SubAgentContractError):
        append_gate_event(
            store,
            execution_id=EXECUTION,
            workspace_id=WORKSPACE,
            event_type=ExecutionEventType.SUB_AGENT_SPAWNED,
            occurred_at=NOW,
            from_phase="a",
            to_phase="b",
            gate="g",
            reason="r",
        )


def test_tree_projection_supports_depth_and_keeps_global_cursors() -> None:
    store = InMemoryExecutionEventStore()
    store.append(
        ExecutionEvent(
            execution_id=EXECUTION,
            workspace_id=WORKSPACE,
            event_type=ExecutionEventType.WORKFLOW_LAUNCHED,
            correlation_id=EXECUTION,
            actor_id="actor",
            actor_scope=ActorScope.WORKFLOW,
            occurred_at=NOW,
            payload={},
        )
    )
    first = _spawn(store, "a", None)
    second = _spawn(store, "b", "a")
    third = _spawn(store, "c", "b")
    append_sub_agent_event(
        store,
        execution_id=EXECUTION,
        workspace_id=WORKSPACE,
        event_type=ExecutionEventType.SUB_AGENT_PROGRESS,
        occurred_at=NOW,
        sub_agent_id="c",
        parent_sub_agent_id="b",
        phase="ideation",
        role="orchestrator",
        context=SubAgentContextSpec(),
        extra={"kind": "tool", "toolCall": {"name": "backtest"}},
    )

    tree = SubAgentTree.from_events(EXECUTION, store.replay(EXECUTION).events)

    assert first.cursor == 2
    assert tree.roots[0].sub_agent_id == "a"
    node_a = tree.get("a")
    node_b = tree.get("b")
    assert node_a is not None
    assert node_b is not None
    assert node_a.children[0].sub_agent_id == "b"
    assert node_b.children[0].sub_agent_id == "c"
    assert tree.subtree_ids("a") == frozenset({"a", "b", "c"})
    assert tree.own_ids("c") == frozenset({"c"})

    page = store.replay(EXECUTION)
    own = filter_replay(page, tree.own_ids("c"))
    assert [event.cursor for event in own.events] == [third.cursor, third.cursor + 1]
    assert own.after_cursor == 0
    assert own.next_cursor == third.cursor + 1

    subtree = filter_replay(page, tree.subtree_ids("b"))
    assert [event.cursor for event in subtree.events] == [
        second.cursor,
        third.cursor,
        third.cursor + 1,
    ]

    resumed = filter_replay(
        store.replay(EXECUTION, after_cursor=second.cursor), tree.own_ids("c")
    )
    assert resumed.after_cursor == second.cursor
    assert [event.cursor for event in resumed.events] == [
        third.cursor,
        third.cursor + 1,
    ]
    assert resumed.next_cursor == third.cursor + 1


def test_needs_input_sets_accepts_input_until_a_gate_resolves() -> None:
    store = InMemoryExecutionEventStore()
    _spawn(store, "a", None)
    append_sub_agent_event(
        store,
        execution_id=EXECUTION,
        workspace_id=WORKSPACE,
        event_type=ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
        occurred_at=NOW,
        sub_agent_id="a",
        parent_sub_agent_id=None,
        phase="hypothesis",
        role="orchestrator",
        context=SubAgentContextSpec(),
        accepts_input=True,
        extra={"question": "provide the falsifier threshold"},
    )

    waiting = SubAgentTree.from_events(EXECUTION, store.replay(EXECUTION).events)
    waiting_node = waiting.get("a")
    assert waiting_node is not None
    assert waiting_node.accepts_input is True
    assert waiting_node.status == STATUS_NEEDS_INPUT

    append_gate_event(
        store,
        execution_id=EXECUTION,
        workspace_id=WORKSPACE,
        event_type=ExecutionEventType.WORKFLOW_GATE_RESOLVED,
        occurred_at=NOW,
        from_phase="hypothesis",
        to_phase="falsification_plan",
        gate="falsifiability",
        reason="threshold supplied",
        sub_agent_id="a",
    )

    resolved = SubAgentTree.from_events(EXECUTION, store.replay(EXECUTION).events)
    resolved_node = resolved.get("a")
    assert resolved_node is not None
    assert resolved_node.accepts_input is False
