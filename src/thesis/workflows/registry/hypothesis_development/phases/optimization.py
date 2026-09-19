"""Strategy optimization phase executor."""

from __future__ import annotations

from dataclasses import replace

from thesis.experiments.ledger import AttemptKind

from ..contracts.creation import HypothesisDevelopmentRequest
from ..errors import (
    WorkflowError,
)
from ..jobs import backtest
from ..ledger import append_attempt
from ..services import DevelopmentEngine
from ..state import HypothesisDevelopmentState


def optimize(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    bundle = state.strategy_bundle
    if bundle is None:
        raise WorkflowError("optimization requires a validated strategy bundle")
    result = backtest(
        workflow,
        request,
        state,
        bundle=bundle,
        # Scope the run id to the bundle revision so a revised strategy
        # never adopts or overwrites the previous revision's run.
        correlation_id=(f"optimize-{request.execution_id}-{bundle.bundle_sha256[:12]}"),
        label="optimization",
    )
    state = replace(state, optimization_backtest=result)
    append_attempt(
        workflow,
        request,
        state,
        AttemptKind.VARIANT,
        "strategy optimization run recorded",
        {"runId": result.job_id, "state": result.state},
    )
    return state
