"""Falsification-trials phase executor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from thesis.experiments.ledger import AttemptKind
from thesis.research.hypothesis import HypothesisDocument
from thesis.workflows.core.phase import HandoffContext
from thesis.workflows.research_models import (
    ResearchModelRunResult,
)

from ..artifacts import (
    build_hypothesis,
    falsification_report_artifact,
    project_hypothesis,
)
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
from ..jobs import run_research_model
from ..ledger import append_attempt
from ..primitives import (
    _DEFAULT_HORIZON,
    _DEFAULT_RISK_FACTOR,
    _digest,
    _metric,
)
from ..services import DevelopmentEngine, services_of
from ..state import HypothesisDevelopmentState
from . import (
    _FALSIFICATION_TRIALS,
    _VERDICT,
)


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
    verdict.  No agent is spawned: this phase is a computation.  Its progress
    and its outcome are workflow-scoped events (REQ-C2): the report artifact
    lands as ``workflow.phase_result``, and a failed computation records the
    error variant before propagating.
    """
    phase = _FALSIFICATION_TRIALS.value
    try:
        contract, final = _run_falsification_trials(workflow, request, state, context)
    except Exception as error:  # noqa: BLE001 - the failure is the datum
        workflow.emit_phase_result(
            request,
            phase,
            summary=f"Falsification trials failed: {error or type(error).__name__}",
            artifact={
                "type": "error",
                "message": str(error) or type(error).__name__,
            },
        )
        raise
    artifact = falsification_report_artifact(workflow, final)
    workflow.emit_phase_result(
        request,
        phase,
        summary=str(artifact.get("content") or f"Ran {len(final.trials)} trial(s)."),
        artifact=artifact,
    )
    return contract


def _run_falsification_trials(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> tuple[Mapping[str, object], HypothesisDevelopmentState]:
    """Run the trials computation and return its contract and final state."""
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
    final = run_trials(workflow, request, staged)
    contract: Mapping[str, object] = {
        "trials": [dict(trial.as_dict()) for trial in final.trials],
        "target": _VERDICT.value,
    }
    return contract, final


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
    # Scope the run id to the hypothesis revision so a revised model never
    # adopts or overwrites the previous revision's run.
    model_run_id = (
        f"falsify-{request.execution_id}-{document.content_sha256[:12]}-model"
    )
    pending = [test for test in plan.tests if test.test_id not in retained]
    if pending:
        # The study runs to completion — the research runner polls until the
        # harness reports the job terminal — and a harness failure or an
        # unusable study result is the phase's failure: the exception (or the
        # WorkflowError below) escapes to the phase executor, which records
        # the error variant before propagating.  There is never a silent
        # all-failed-trial completion.
        model_run = run_research_model(
            workflow,
            request,
            state,
            model=state.draft.model,
            correlation_id=model_run_id,
            label="layer-a-model",
        )
        result = model_run.backtest
        if not result.succeeded:
            detail = result.error or f"harness run ended in state {result.state!r}"
            raise WorkflowError(f"the falsification study failed: {detail}")
        metrics = result.metrics
        if not isinstance(metrics, Mapping) or not metrics:
            raise WorkflowError(
                "the falsification study returned no usable metrics, so the "
                "planned falsifiers cannot be evaluated"
            )
        missing = sorted(
            {
                test.observable
                for test in pending
                if _metric(metrics, test.observable) is None
            }
        )
        if missing:
            raise WorkflowError(
                "the falsification study metrics do not carry the planned "
                f"observable(s): {', '.join(missing)}"
            )
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
        assert model_run is not None
        result = model_run.backtest
        # Every planned falsifier observes its actual metric value — the
        # data-failure falsifiers included — so a fired falsifier records
        # what really happened instead of collapsing into no observation.
        observed = _metric(result.metrics, test.observable)
        falsifier = falsifiers[test.falsifier_id]
        trials.append(
            FalsificationTrial(
                test_id=test.test_id,
                falsifier_id=test.falsifier_id,
                run_id=result.job_id,
                digest=_digest(result.as_dict()),
                run_state=result.state,
                observed=observed,
                falsified=(
                    falsifier.falsifies(observed) if observed is not None else False
                ),
                detail=result.error,
            )
        )
        append_attempt(
            workflow,
            request,
            state,
            AttemptKind.METRIC,
            f"falsification trial {test.test_id} recorded",
            {
                "detail": result.error,
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
