"""Workflow-scoped event vocabulary: append + replay round-trips (REQ-C2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from thesis.execution.events import (
    ActorScope,
    ExecutionEventType,
    InMemoryExecutionEventStore,
)
from thesis.execution.workflow_events import (
    WorkflowEventContractError,
    append_workflow_phase_result,
    append_workflow_progress,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def test_progress_events_append_and_replay_without_a_sub_agent_id() -> None:
    store = InMemoryExecutionEventStore()

    event = append_workflow_progress(
        store,
        execution_id="execution-1",
        workspace_id="workspace-demo",
        occurred_at=NOW,
        phase="falsification_trials",
        kind="activity",
        text="Running the planned falsification trials.",
    )

    assert event.event_type is ExecutionEventType.WORKFLOW_PROGRESS
    assert event.actor_scope is ActorScope.WORKFLOW
    assert event.payload == {
        "phase": "falsification_trials",
        "kind": "activity",
        "text": "Running the planned falsification trials.",
    }
    assert "subAgentId" not in event.payload

    replayed = store.replay("execution-1")
    assert len(replayed) == 1
    assert replayed[0].event_type is ExecutionEventType.WORKFLOW_PROGRESS
    assert replayed[0].payload == event.payload


def test_progress_kind_defaults_to_activity_and_text_is_optional() -> None:
    store = InMemoryExecutionEventStore()

    event = append_workflow_progress(
        store,
        execution_id="execution-1",
        workspace_id="workspace-demo",
        occurred_at=NOW,
        phase="verdict",
    )

    assert event.payload == {"phase": "verdict", "kind": "activity"}


def test_phase_result_events_carry_phase_summary_and_artifact() -> None:
    store = InMemoryExecutionEventStore()
    artifact = {"type": "falsification_report", "trials": [{"testId": "t1"}]}

    event = append_workflow_phase_result(
        store,
        execution_id="execution-1",
        workspace_id="workspace-demo",
        occurred_at=NOW,
        phase="falsification_trials",
        summary="Ran 1 planned trial: 1 survived.",
        artifact=artifact,
    )

    assert event.event_type is ExecutionEventType.WORKFLOW_PHASE_RESULT
    assert event.payload["phase"] == "falsification_trials"
    assert event.payload["summary"] == "Ran 1 planned trial: 1 survived."
    assert event.payload["artifact"] == artifact
    assert "subAgentId" not in event.payload

    replayed = store.replay("execution-1")
    assert replayed[0].payload["artifact"] == artifact


def test_payload_violations_fail_closed() -> None:
    store = InMemoryExecutionEventStore()

    with pytest.raises(WorkflowEventContractError):
        append_workflow_progress(
            store,
            execution_id="execution-1",
            workspace_id="workspace-demo",
            occurred_at=NOW,
            phase="  ",
        )
    with pytest.raises(WorkflowEventContractError):
        append_workflow_progress(
            store,
            execution_id="execution-1",
            workspace_id="workspace-demo",
            occurred_at=NOW,
            phase="verdict",
            text="   ",
        )
    with pytest.raises(WorkflowEventContractError):
        append_workflow_phase_result(
            store,
            execution_id="execution-1",
            workspace_id="workspace-demo",
            occurred_at=NOW,
            phase="verdict",
            summary=" ",
            artifact={},
        )
    with pytest.raises(WorkflowEventContractError):
        append_workflow_phase_result(
            store,
            execution_id="execution-1",
            workspace_id="workspace-demo",
            occurred_at=NOW,
            phase="verdict",
            summary="done",
            artifact="not-a-mapping",  # type: ignore
        )
    # Nothing was appended by the rejected calls.
    assert store.latest_cursor("execution-1") == 0
