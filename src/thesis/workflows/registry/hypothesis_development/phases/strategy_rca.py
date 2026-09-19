"""Strategy root-cause phase executors."""

from __future__ import annotations

import json
from dataclasses import replace

from thesis.experiments.ledger import AttemptKind

from ..contracts.creation import HypothesisDevelopmentRequest
from ..errors import (
    WorkflowError,
)
from ..ledger import append_attempt
from ..primitives import (
    _RCA_SCHEMA,
)
from ..services import DevelopmentEngine, services_of
from ..state import HypothesisDevelopmentState
from . import (
    _STRATEGY_RCA,
)


def diagnose_strategy(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Ask the persistent strategy RCA node to diagnose one failed run."""
    result = state.strategy_backtest or state.optimization_backtest
    if result is None:
        raise WorkflowError("strategy RCA requires a failed backtest")
    active = workflow.active_orchestrators.get(request.execution_id)
    if active is None or active.phase is not _STRATEGY_RCA:
        raise WorkflowError("strategy RCA orchestrator is not active")
    prompt_for = workflow._prompt_builder(
        workspace_scope=request.workspace_id,
        role=active.role,
        context_id=f"{request.execution_id}-strategy-rca-{state.strategy_cycles}",
    )
    task = "\n".join(
        [
            "Diagnose this failed strategy backtest. Separate harness/data defects "
            "from implementation, strategy, or market-regime defects. Return a "
            "concrete revision handoff for the strategy-design agent.",
            "Set falsifierId to strategy_backtest and choose a concise failureClass.",
            "In the rationale and follow-ups, render any mathematics as LaTeX for "
            "KaTeX: inline math as \\(...\\) and display equations as \\[...\\] "
            "on their own line. Never use a bare $ for math.",
            f"Strategy attempt: {state.strategy_cycles}",
            f"Backtest result: {json.dumps(result.as_dict(), default=str, sort_keys=True)}",
            f"Backtest metadata: {json.dumps(dict(result.metadata), default=str, sort_keys=True)}",
            f"Strategy manifest: {json.dumps(state.strategy_bundle.manifest() if state.strategy_bundle else {}, default=str, sort_keys=True)}",
            f"Required schema: {_RCA_SCHEMA}",
        ]
    )
    diagnosis, _ = workflow._compose_with_repair(
        request,
        services_of(workflow).critique_role,
        base_task=task,
        prompt_for=prompt_for,
        compose=services_of(workflow).rca_composer.compose,
        response_schema=_RCA_SCHEMA,
        emit_output=False,
    )
    diagnosis = replace(diagnosis, falsifier_id="strategy_backtest")
    prose = "\n".join(
        [
            "### Strategy Backtest RCA",
            "",
            diagnosis.rationale,
            "",
            *[f"- {follow_up}" for follow_up in diagnosis.follow_ups],
        ]
    )
    workflow.emit_orchestrator_text(request, prose)
    append_attempt(
        workflow,
        request,
        state,
        AttemptKind.ABORTED_JOB,
        "strategy RCA diagnosis recorded",
        diagnosis.as_dict(),
    )
    return replace(
        state,
        rca=(*state.rca, diagnosis),
    )


def strategy_handoff(
    workflow: DevelopmentEngine, state: HypothesisDevelopmentState
) -> str:
    """Render the latest strategy RCA finding for the next design attempt."""
    diagnosis = state.rca[-1]
    lines = [
        f"Strategy RCA handoff after attempt {state.strategy_cycles}:",
        f"Failure class: {diagnosis.failure_class}",
        diagnosis.rationale,
        "Required revisions:",
        *[f"- {item}" for item in diagnosis.follow_ups],
    ]
    return "\n".join(lines)
