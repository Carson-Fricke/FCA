"""Generic workflow event projections.

Phase entry and execution completion are the engine's durable projections of
workflow progress.  Both append the same typed execution events the UI reads;
neither knows a workflow-specific payload beyond the phase value the caller
supplies.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from thesis_runtime.contracts.records import (
    RecordNotFound,
    WorkflowExecutionState,
)
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.execution.events import (
    ActorScope,
    ExecutionEvent,
    ExecutionEventType,
)
from thesis_runtime.workflows.graph import Graph


def graph_payload(graph: Graph) -> dict[str, object]:
    """Present a frontier and pending joins; this is never execution authority."""
    return {
        "activePhases": list(graph.positions),
        "controlSequence": graph.sequence,
        "joins": {
            region.split.join: {
                "arrived": list(region.arrivals),
                "required": list(region.split.branches),
                "splitAttemptId": key,
            }
            for key, region in graph.regions.items()
        },
    }


def emit_phase(
    *,
    chat: ChatService,
    workspace_id: str,
    execution_id: str,
    phase: str,
    payload: Mapping[str, object],
    actor_id: str,
    intent_epoch: int,
    clock: Callable[[], datetime],
    id_factory: Callable[[str], str],
) -> None:
    """Append one ``workflow.phase`` event for ``phase``."""
    chat.event_store.append(
        ExecutionEvent(
            execution_id=execution_id,
            workspace_id=workspace_id,
            event_type=ExecutionEventType.WORKFLOW_PHASE,
            correlation_id=execution_id,
            actor_id=actor_id,
            actor_scope=ActorScope.WORKFLOW,
            occurred_at=clock(),
            payload={"phase": phase, **dict(payload)},
            intent_epoch=intent_epoch,
            event_id=id_factory("event"),
        )
    )


def finish_execution(
    *,
    chat: ChatService,
    workspace_id: str,
    execution_id: str,
    final_state: WorkflowExecutionState,
    event_type: ExecutionEventType,
    payload: Mapping[str, object] | None = None,
    actor_id: str,
    clock: Callable[[], datetime],
    id_factory: Callable[[str], str],
    on_finished: Callable[[], None] | None = None,
) -> None:
    """Mark the durable execution terminal and append its final event.

    A missing or already-terminal execution is a no-op, so a delayed delivery
    cannot fail a run that another turn already finished.  ``on_finished``
    closes the execution's owning workflow node after the event lands.
    """
    try:
        accepted = chat.finish_workflow(
            workspace_id,
            execution_id,
            actor_id=actor_id,
            state=final_state,
            event_type=event_type,
            payload=payload,
        )
    except RecordNotFound:
        return
    if accepted and on_finished is not None:
        on_finished()
