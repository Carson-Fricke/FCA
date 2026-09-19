"""Strategy-backtesting pipeline executors."""

from __future__ import annotations

from dataclasses import replace

from thesis.chat.contracts import (
    WorkflowExecutionState,
)
from thesis.execution.events import (
    ExecutionEventType,
)
from thesis.experiments.ledger import AttemptKind
from thesis.research.hypothesis import (
    HypothesisDocument,
)
from thesis.workflows.backtests import (
    BacktestRunError,
    BacktestRunResult,
    HarnessError,
)

from ..artifacts import completion_payload
from ..contracts.creation import HypothesisDevelopmentRequest
from ..errors import (
    WorkflowError,
)
from ..gates.strategy import check_strategy_design
from ..jobs import backtest, stop_for_cycle_budget
from ..ledger import append_attempt, journal_entry
from ..services import DevelopmentEngine, services_of
from ..state import HypothesisDevelopmentState
from . import (
    _COMPLETED,
    _OPTIMIZATION,
    _STRATEGY_BACKTESTING,
    _STRATEGY_DESIGN,
    _STRATEGY_RCA,
)
from .design import advance_design
from .optimization import optimize
from .strategy_rca import diagnose_strategy, strategy_handoff


def run_strategy_pipeline(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Run design, backtesting, RCA, and optimization to a terminal outcome."""
    document = state.hypothesis
    if document is None:
        raise WorkflowError("strategy design requires a hypothesis")
    if state.phase is not _STRATEGY_DESIGN:
        state = workflow._transition(
            request,
            state,
            _STRATEGY_DESIGN,
            "hypothesis survived falsification",
        )
    state = advance_design(workflow, request, state)
    before = state.phase
    state = workflow._transition_checked(
        request,
        state,
        _STRATEGY_BACKTESTING,
        "strategy validated with falsifier exit criteria",
        lambda: check_strategy_design(
            state.strategy_bundle, state.exit_criteria, document.front_matter
        ),
    )
    if state.phase is before:
        # The design gate delivered its reason as an operator request; the
        # next turn re-designs once the operator has answered.
        return state
    state = run_strategy_backtest(workflow, request, state)
    while True:
        state = revise_until_strategy_test_passes(workflow, request, state, document)
        if state.phase is not _STRATEGY_BACKTESTING:
            return state
        state = workflow._transition(
            request,
            state,
            _OPTIMIZATION,
            "strategy backtest completed successfully",
        )
        state = optimize(workflow, request, state)
        if state.optimization_backtest is None or state.optimization_backtest.succeeded:
            break
        state = replace(state, strategy_backtest=state.optimization_backtest)
    journal_id = journal_entry(workflow, request, state)
    state = replace(state, journal_id=journal_id)
    workflow._finish_execution(
        request,
        WorkflowExecutionState.COMPLETED,
        ExecutionEventType.WORKFLOW_COMPLETED,
        payload=completion_payload(workflow, request, state),
    )
    return workflow._transition(request, state, _COMPLETED, "outcome recorded")


def run_strategy_backtest(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Run one strategy attempt and retain a failed result for RCA."""
    bundle = state.strategy_bundle
    if bundle is None:
        raise WorkflowError("strategy backtesting requires a strategy bundle")
    run_id = f"strategy-test-{request.execution_id}-{state.strategy_cycles}"
    try:
        result = backtest(
            workflow,
            request,
            state,
            bundle=bundle,
            correlation_id=run_id,
            label="strategy-backtesting",
        )
    except (BacktestRunError, HarnessError) as error:
        result = BacktestRunResult(
            job_id=run_id,
            state="failed",
            metadata={"error": str(error)},
        )
    append_attempt(
        workflow,
        request,
        state,
        AttemptKind.VARIANT,
        "strategy backtest recorded",
        result.as_dict(),
    )
    return replace(state, strategy_backtest=result)


def revise_until_strategy_test_passes(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    document: HypothesisDocument,
) -> HypothesisDevelopmentState:
    """Run bounded strategy RCA/design cycles until one test succeeds."""
    while state.strategy_backtest is not None and not state.strategy_backtest.succeeded:
        state = workflow._transition(
            request,
            state,
            _STRATEGY_RCA,
            "strategy backtest failed; diagnosing the implementation",
        )
        state = diagnose_strategy(workflow, request, state)
        if state.strategy_cycles >= services_of(workflow).max_rca_cycles:
            return stop_for_cycle_budget(
                workflow,
                request,
                state,
                purpose="recover from an exhausted strategy revision budget",
                detail=(
                    "the strategy revision budget of 5 attempts is exhausted "
                    "after repeated failed backtests; review the backtest "
                    "lineage and provide operator direction"
                ),
            )
        state = workflow._transition(
            request,
            state,
            _STRATEGY_DESIGN,
            "strategy RCA recommendations ready for revision",
        )
        workflow._post_phase_handoff(
            request,
            strategy_handoff(workflow, state),
            kind="strategy_rca_findings",
        )
        state = advance_design(workflow, request, state)
        state = workflow._transition_checked(
            request,
            state,
            _STRATEGY_BACKTESTING,
            "revised strategy validated with falsifier exit criteria",
            lambda current=state: check_strategy_design(
                current.strategy_bundle,
                current.exit_criteria,
                document.front_matter,
            ),
        )
        state = run_strategy_backtest(workflow, request, state)
    return state
