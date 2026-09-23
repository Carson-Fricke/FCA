"""Attempt-ledger projection for the workflow."""

from __future__ import annotations

from collections.abc import Mapping

from thesis.experiments.ledger import AttemptKind, AttemptRecord
from thesis.persistence.journal import content_hash

from .contracts import (
    HypothesisDevelopmentRequest,
)
from .primitives import (
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
