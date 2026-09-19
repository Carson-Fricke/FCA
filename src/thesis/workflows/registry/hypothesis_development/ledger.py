"""Attempt-ledger and journal projections for the workflow."""

from __future__ import annotations

from collections.abc import Mapping

from thesis.experiments.ledger import AttemptKind, AttemptRecord
from thesis.persistence.journal import ArtifactRef, content_hash

from .contracts import (
    HypothesisDevelopmentRequest,
)
from .phases import (
    _COMPLETED,
)
from .primitives import (
    _HYPOTHESIS_MEDIA_TYPE,
    _WORKFLOW_ACTOR,
    _WORKFLOW_ID,
    _WORKFLOW_VERSION,
    _iso,
)
from .services import DevelopmentEngine, services_of
from .state import HypothesisDevelopmentState


def append_attempt(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    kind: AttemptKind,
    description: str,
    payload: Mapping[str, object],
) -> None:
    ledger = services_of(workflow).ledger
    if ledger is None:
        return
    contract_sha256 = (
        state.hypothesis.content_sha256
        if state.hypothesis is not None
        else content_hash({"id": _WORKFLOW_ID, "version": _WORKFLOW_VERSION})
    )
    sequence = len(ledger.entries()) + 1
    ledger.append(
        AttemptRecord(
            attempt_id=f"attempt-{request.execution_id}-{sequence}",
            contract_sha256=contract_sha256,
            kind=kind,
            description=description,
            actor=_WORKFLOW_ACTOR,
            observed_at=_iso(workflow.clock()),
            payload=dict(payload),
        )
    )


def journal_entry(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> str | None:
    journal = services_of(workflow).journal
    if journal is None:
        return None
    definition = workflow.definition
    journal_id = f"workflow-{request.execution_id}"
    artifacts: tuple[ArtifactRef, ...] = ()
    if state.hypothesis is not None:
        artifacts = (
            ArtifactRef.from_bytes(
                state.hypothesis.relative_path,
                state.hypothesis.to_bytes(),
                media_type=_HYPOTHESIS_MEDIA_TYPE,
            ),
        )
    journal.accept(
        "workflow.hypothesis_development.completed",
        {
            "executionId": request.execution_id,
            "requestedBy": request.requested_by,
            "strategyIds": []
            if state.strategy_bundle is None
            else [state.strategy_bundle.strategy_id],
            "triggerMessageId": request.trigger_message_id,
            "workflow": {
                "id": definition.workflow_id,
                "version": definition.version,
            },
        },
        {
            "phase": _COMPLETED.value,
            "verdict": None if state.verdict is None else state.verdict.as_dict(),
        },
        workflow_hash=content_hash(
            {"id": definition.workflow_id, "version": definition.version}
        ),
        model_hash=content_hash(dict(state.model_provenance or {})),
        artifacts=artifacts,
        idempotency_id=journal_id,
    )
    return journal_id
