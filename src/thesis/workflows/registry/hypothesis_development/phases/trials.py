"""Falsification-trials phase executor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from thesis.experiments.ledger import AttemptKind
from thesis.research.hypothesis import HypothesisDocument
from thesis.workflows.backtests import (
    BacktestRunError,
    BacktestRunTimeout,
    HarnessError,
)
from thesis.workflows.core.phase import HandoffContext
from thesis.workflows.research_models import (
    ResearchModelRunResult,
)

from ..artifacts import build_hypothesis, project_hypothesis
from ..composers.hypothesis import (
    _compose_falsifiers,
    _compose_research_model,
    _compose_universe,
    _optional_compose_text,
)
from ..contracts.creation import HypothesisDevelopmentRequest
from ..contracts.hypothesis import HypothesisDraft
from ..contracts.plan import FalsificationPlan, FalsifierTest
from ..contracts.trials import FalsificationTrial
from ..errors import (
    FalsificationComposeError,
    WorkflowError,
)
from ..gates.evidence import check_evidence_bound
from ..jobs import run_research_model
from ..ledger import append_attempt
from ..primitives import (
    _DEFAULT_HORIZON,
    _DEFAULT_RISK_FACTOR,
    _digest,
    _metric,
)
from ..prompts.falsification import _falsification_turn_prompt
from ..services import DevelopmentEngine, services_of
from ..state import HypothesisDevelopmentState
from . import (
    _VERDICT,
    _ActiveSubAgent,
)
from .verdict import emit_verdict_review, evaluate_verdict


def run_falsification_trials(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> Mapping[str, object] | None:
    """Run the falsification trials deterministically and return the contract.

    The accepted hypothesis contract is composed into the typed document and
    falsification plan, the research model runs once through the harness, and
    each planned falsifier is recorded as a trial with its observed value and
    verdict.  No agent is spawned: this phase is a computation.
    """
    payload = context.completion.get("hypothesis")
    if not isinstance(payload, Mapping):
        raise WorkflowError(
            "falsification trials require the accepted hypothesis contract"
        )
    draft = _draft_from_contract(payload)
    document = build_hypothesis(workflow, request, draft)
    services_of(workflow).store.save(document)
    plan = _plan_from_document(document)
    staged = replace(state, draft=draft, hypothesis=document, plan=plan)
    try:
        final = run_trials(workflow, request, staged)
    except BacktestRunTimeout as error:
        raise WorkflowError(
            f"the falsification model run has not reached a terminal state: {error}"
        ) from error
    return {
        "trials": [dict(trial.as_dict()) for trial in final.trials],
        "target": _VERDICT.value,
    }


def _draft_from_contract(payload: Mapping[str, object]) -> HypothesisDraft:
    """Compose the typed hypothesis draft from the accepted contract payload."""
    try:
        model = _compose_research_model(payload.get("model"))
        return HypothesisDraft(
            claim=_required_text(payload, "claim"),
            disagreement=_required_text(payload, "disagreement"),
            risk_factor=_optional_compose_text(
                payload, "riskFactor", _DEFAULT_RISK_FACTOR, max_length=4_096
            ),
            horizon=_optional_compose_text(
                payload, "horizon", _DEFAULT_HORIZON, max_length=4_096
            ),
            universe=_compose_universe(payload.get("universe"), model),
            falsifiers=_compose_falsifiers(payload.get("falsifiers")),
            model=model,
            sizing=None,
            body=_optional_text(payload.get("body")),
            hypothesis_id=None,
        )
    except WorkflowError as error:
        raise FalsificationComposeError(str(error)) from error


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FalsificationComposeError(f"hypothesis requires {key!r}")
    return value.strip()


def _optional_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _plan_from_document(document: HypothesisDocument) -> FalsificationPlan:
    """Derive one falsifier test per declared falsifier."""
    front = document.front_matter
    tests: list[FalsifierTest] = []
    for index, falsifier in enumerate(front.falsifiers, start=1):
        tests.append(
            FalsifierTest(
                test_id=f"T{index:02d}",
                falsifier_id=str(falsifier.falsifier_id),
                observable=falsifier.observable,
                condition=falsifier.condition.value
                if hasattr(falsifier.condition, "value")
                else str(falsifier.condition),
                threshold=falsifier.threshold,
                description=falsifier.test,
            )
        )
    return FalsificationPlan(tests=tuple(tests), controls=())


def advance_trials(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Run the bounded premise and strategy loops one turn at a time."""
    document = state.hypothesis
    if document is None:
        raise WorkflowError("falsification trials require a hypothesis")
    state = run_trials(workflow, request, state)
    if not state.trials:
        # The research model has not reached a terminal state; the timeout
        # was delivered as an ordinary operator request on the trials node.
        return state
    # The agent evaluates the trials first, in prose, including any harness
    # or data failure, before the evidence gate decides whether the verdict
    # can be trusted. This is the agent-first replacement for a separate
    # falsification RCA step and guarantees failures reach the model.
    active = workflow.active_orchestrators.get(request.execution_id)
    if active is not None:
        prose = author_falsification_prose(workflow, request, state, active)
        state = replace(state, falsification_prose=prose or None)
        if prose:
            workflow.emit_orchestrator_text(request, prose)
    before = state.phase
    state = workflow._transition_checked(
        request,
        state,
        _VERDICT,
        "evidence bound to every planned falsifier",
        lambda: check_evidence_bound(state.plan, state.trials),
    )
    if state.phase is before:
        # The evidence gate delivered its reason as an operator request;
        # the next turn re-checks it once the operator has answered.
        return state
    verdict = evaluate_verdict(workflow, state)
    state = project_hypothesis(workflow, request, state, status=verdict.outcome)
    state = replace(state, verdict=verdict)
    append_attempt(
        workflow,
        request,
        state,
        AttemptKind.METRIC,
        "falsification verdict recorded",
        verdict.as_dict(),
    )
    emit_verdict_review(workflow, request, state)
    return state


def run_trials(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    document = state.hypothesis
    plan = state.plan
    if document is None or plan is None or state.draft is None:
        raise WorkflowError("trials require a hypothesis and a falsification plan")
    falsifiers = {
        falsifier.falsifier_id: falsifier
        for falsifier in document.front_matter.falsifiers
        if falsifier.falsifier_id is not None
    }
    retained = {
        trial.test_id: trial for trial in state.trials if trial.observed is not None
    }
    trials: list[FalsificationTrial] = []
    model_reference = state.research_model_reference
    model_artifact = state.research_model_artifact
    fitted_parameters = state.fitted_parameters
    runtime_configuration = state.research_model_runtime_configuration
    fitted_reference = state.fitted_model_reference
    model_run: ResearchModelRunResult | None = None
    model_error: str | None = None
    # Scope the run id to the hypothesis revision so a revised model never
    # adopts or overwrites the previous revision's run.
    model_run_id = (
        f"falsify-{request.execution_id}-{document.content_sha256[:12]}-model"
    )
    if any(test.test_id not in retained for test in plan.tests):
        try:
            model_run = run_research_model(
                workflow,
                request,
                state,
                model=state.draft.model,
                correlation_id=model_run_id,
                label="layer-a-model",
            )
        except BacktestRunTimeout as error:
            # The run has not finished, so no falsifier has an observation
            # yet. Pause for the operator instead of recording a false
            # refutation for every planned test.
            append_attempt(
                workflow,
                request,
                state,
                AttemptKind.ABORTED_JOB,
                "layer A research model run is still executing",
                {"error": str(error)},
            )
            workflow._request_operator_input(
                request,
                state,
                purpose="wait for the Layer A research model run to finish",
                detail=(
                    "the falsification model run has not reached a terminal state "
                    f"yet ({error}); its market-data backfill or replay may still "
                    "be running, so no falsifier can be evaluated"
                ),
            )
            return state
        except (BacktestRunError, HarnessError, ValueError) as error:
            model_error = str(error)
        else:
            model_reference = model_run.model_reference
            model_artifact = model_run.workspace_artifact
            fitted_parameters = model_run.fitted_parameters
            runtime_configuration = model_run.runtime_configuration
            fitted_reference = model_run.fitted_reference
    for test in plan.tests:
        existing = retained.get(test.test_id)
        if existing is not None:
            trials.append(existing)
            continue
        if model_error is not None:
            trials.append(
                FalsificationTrial(
                    test_id=test.test_id,
                    falsifier_id=test.falsifier_id,
                    run_id=model_run_id,
                    digest=_digest({"error": model_error}),
                    run_state="failed",
                    observed=None,
                    falsified=False,
                    detail=model_error,
                )
            )
            append_attempt(
                workflow,
                request,
                state,
                AttemptKind.ABORTED_JOB,
                f"falsification trial {test.test_id} could not run",
                {
                    "error": model_error,
                    "falsifierId": test.falsifier_id,
                },
            )
            continue
        assert model_run is not None
        result = model_run.backtest
        sample_valid = _metric(result.metrics, "sample_valid")
        if sample_valid is not None and sample_valid <= 0:
            observed = None
        else:
            observed = _metric(result.metrics, test.observable)
        falsifier = falsifiers[test.falsifier_id]
        detail = result.error
        if detail is None and not result.succeeded:
            detail = f"harness run ended in state {result.state!r}"
        trials.append(
            FalsificationTrial(
                test_id=test.test_id,
                falsifier_id=test.falsifier_id,
                run_id=result.job_id,
                digest=_digest(result.as_dict()),
                run_state=result.state,
                observed=observed,
                falsified=observed is not None and falsifier.falsifies(observed),
                detail=detail,
            )
        )
        append_attempt(
            workflow,
            request,
            state,
            AttemptKind.METRIC,
            f"falsification trial {test.test_id} recorded",
            {
                "detail": detail,
                "falsifierId": test.falsifier_id,
                "observed": observed,
                "runId": result.job_id,
                "runState": result.state,
            },
        )
    state = replace(
        state,
        trials=tuple(trials),
        research_model_reference=model_reference,
        research_model_artifact=model_artifact,
        fitted_model_reference=fitted_reference,
        fitted_parameters=fitted_parameters,
        research_model_runtime_configuration=runtime_configuration,
    )
    return project_hypothesis(workflow, request, state)


def author_falsification_prose(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    active: _ActiveSubAgent,
) -> str:
    """Prompt the sub-agent for the falsification phase to produce its prose analysis."""
    services = services_of(workflow)
    prompt_for = workflow._prompt_builder(
        workspace_scope=request.workspace_id,
        role=active.role,
        context_id=f"{request.execution_id}-falsification-prose",
    )
    task = _falsification_turn_prompt(
        state,
        (state.fitted_parameters or {}),
    )
    try:
        assert services.router is not None
        invocation = services.router.invoke(
            services.research_role,
            prompt_for(task),
        )
        return (invocation.output or "").strip()
    except Exception:
        return ""
