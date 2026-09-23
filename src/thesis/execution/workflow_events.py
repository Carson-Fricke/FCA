"""Workflow-scoped event vocabulary and appenders (REQ-C2).

A deterministic phase is not a sub-agent: it has no durable node, no
orchestrator lifecycle, and never emits ``sub_agent.*`` events.  Its executor
records progress as execution-scoped ``workflow.progress`` events and its
outcome as an execution-scoped ``workflow.phase_result`` event — both carrying
the phase name and neither carrying a ``subAgentId``.  A failed computation
records the error variant, not a shrug.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from thesis.execution.events import (
    ActorScope,
    ExecutionEvent,
    ExecutionEventStore,
    ExecutionEventType,
)

WORKFLOW_EVENT_TYPES: frozenset[ExecutionEventType] = frozenset(
    {
        ExecutionEventType.WORKFLOW_PROGRESS,
        ExecutionEventType.WORKFLOW_PHASE_RESULT,
    }
)


class WorkflowEventContractError(ValueError):
    """Raised when a workflow event payload violates the contract."""


def _phase(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowEventContractError("phase must be non-empty text")
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WorkflowEventContractError(f"{name} must be non-empty text when present")
    return value.strip()


def _progress_payload(
    *, phase: object, kind: object, text: object
) -> dict[str, object]:
    payload: dict[str, object] = {
        "phase": _phase(phase),
        "kind": "activity" if kind is None else kind,
    }
    if not isinstance(payload["kind"], str) or not payload["kind"].strip():
        raise WorkflowEventContractError("kind must be non-empty text when present")
    checked = _optional_text(text, "text")
    if checked is not None:
        payload["text"] = checked
    return payload


def _phase_result_payload(
    *, phase: object, summary: object, artifact: object
) -> dict[str, object]:
    summary_text = _optional_text(summary, "summary")
    if summary_text is None:
        raise WorkflowEventContractError(
            "phase_result events require a non-empty summary"
        )
    if not isinstance(artifact, Mapping):
        raise WorkflowEventContractError("phase_result events require an artifact")
    return {
        "phase": _phase(phase),
        "summary": summary_text,
        "artifact": dict(artifact),
    }


def _append(
    store: ExecutionEventStore,
    *,
    execution_id: str,
    workspace_id: str,
    event_type: ExecutionEventType,
    occurred_at: datetime,
    payload: Mapping[str, object],
    actor_id: str,
    actor_scope: ActorScope,
    correlation_id: str | None,
    intent_epoch: int,
) -> ExecutionEvent:
    return store.append(
        ExecutionEvent(
            execution_id=execution_id,
            workspace_id=workspace_id,
            event_type=event_type,
            correlation_id=correlation_id or execution_id,
            actor_id=actor_id,
            actor_scope=actor_scope,
            occurred_at=occurred_at,
            payload=dict(payload),
            intent_epoch=intent_epoch,
        )
    )


def append_workflow_progress(
    store: ExecutionEventStore,
    *,
    execution_id: str,
    workspace_id: str,
    occurred_at: datetime,
    phase: str,
    kind: str | None = None,
    text: str | None = None,
    actor_id: str = "thesis-workflow",
    actor_scope: ActorScope = ActorScope.WORKFLOW,
    correlation_id: str | None = None,
    intent_epoch: int = 0,
) -> ExecutionEvent:
    """Append one execution-scoped ``workflow.progress`` event.

    The payload carries ``{phase, kind?, text?}`` and never a ``subAgentId``.
    """
    return _append(
        store,
        execution_id=execution_id,
        workspace_id=workspace_id,
        event_type=ExecutionEventType.WORKFLOW_PROGRESS,
        occurred_at=occurred_at,
        payload=_progress_payload(phase=phase, kind=kind, text=text),
        actor_id=actor_id,
        actor_scope=actor_scope,
        correlation_id=correlation_id,
        intent_epoch=intent_epoch,
    )


def append_workflow_phase_result(
    store: ExecutionEventStore,
    *,
    execution_id: str,
    workspace_id: str,
    occurred_at: datetime,
    phase: str,
    summary: str,
    artifact: Mapping[str, object],
    actor_id: str = "thesis-workflow",
    actor_scope: ActorScope = ActorScope.WORKFLOW,
    correlation_id: str | None = None,
    intent_epoch: int = 0,
) -> ExecutionEvent:
    """Append one execution-scoped ``workflow.phase_result`` event.

    The payload carries ``{phase, summary, artifact}`` and never a
    ``subAgentId``.
    """
    return _append(
        store,
        execution_id=execution_id,
        workspace_id=workspace_id,
        event_type=ExecutionEventType.WORKFLOW_PHASE_RESULT,
        occurred_at=occurred_at,
        payload=_phase_result_payload(phase=phase, summary=summary, artifact=artifact),
        actor_id=actor_id,
        actor_scope=actor_scope,
        correlation_id=correlation_id,
        intent_epoch=intent_epoch,
    )


__all__ = [
    "WORKFLOW_EVENT_TYPES",
    "WorkflowEventContractError",
    "append_workflow_phase_result",
    "append_workflow_progress",
]
