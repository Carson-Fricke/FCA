"""Falsification-trials phase executor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from thesis.chat.contracts import HARNESS_RESEARCH_MODEL_TOOL
from thesis.experiments.mandate import derive_launch_mandate, rfc3339_ns
from thesis.research.hypothesis import HypothesisDocument
from thesis.research.models import ResearchModel
from thesis.workflows.core.phase import HandoffContext
from thesis.workflows.research_models import (
    ResearchModelRunResult,
    StudyProgramReference,
    resolve_study_reference,
    study_window,
)
from thesis.workspaces.sketch_contract import StrategySketchReference

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
from ..contracts.evidence import (
    AssumptionEvidence,
    ControlEvidence,
    ResearchEvidence,
)
from ..contracts.hypothesis import HypothesisDraft
from ..contracts.plan import FalsificationPlan, FalsifierTest
from ..contracts.trials import FalsificationTrial
from ..errors import (
    FalsificationComposeError,
    WorkflowError,
)
from ..jobs import research_model_attribution, run_research_model
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
        contract, final, artifact = _run_falsification_trials(
            workflow, request, state, context
        )
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
) -> tuple[Mapping[str, object], HypothesisDevelopmentState, Mapping[str, object]]:
    """Run the trials computation and return its contract, state, and report."""
    payload = context.completion.get("hypothesis")
    if not isinstance(payload, Mapping):
        raise WorkflowError(
            "falsification trials require the accepted hypothesis contract"
        )
    # The pure fold owns historical decoding. A newly supplied handoff never
    # enables the retired inline schema just because it reaches an executor.
    draft = state.draft or _draft_from_contract(payload)
    if draft.sketch_reference is None:
        raise WorkflowError("falsification requires an accepted sketch reference")
    sketch_reference = draft.sketch_reference
    if (
        state.discovery is None
        or state.discovery.sketch_reference != draft.sketch_reference
    ):
        raise WorkflowError("falsification sketch does not match accepted discovery")
    programs = services_of(workflow).programs
    if programs is None:
        raise WorkflowError("workspace programs are not configured")
    sketch = programs.verify_strategy_sketch(
        request.workspace_id, draft.sketch_reference.document()
    )
    assumptions = sketch.get("assumptions")
    assert isinstance(assumptions, list)
    known_assumptions = {item["assumptionId"] for item in assumptions}
    unknown = set(draft.assumption_tests) - known_assumptions
    if unknown:
        raise WorkflowError(
            f"falsification names unknown sketch assumptions: {sorted(unknown)}"
        )
    known_falsifiers = {item.falsifier_id for item in draft.falsifiers}
    for assumption, tests in draft.assumption_tests.items():
        if set(tests) - known_falsifiers:
            raise WorkflowError(
                f"assumption {assumption!r} references unknown falsifier IDs"
            )
    accepted_study: StudyProgramReference | None = None
    resolved_study: ResearchModel | None = None
    if isinstance(draft.model, StudyProgramReference):
        accepted_study = draft.model
        programs = services_of(workflow).programs
        if programs is None:
            raise WorkflowError("workspace programs are not configured")
        resolved_study = resolve_study_reference(
            programs, request.workspace_id, draft.model
        )
        draft = replace(draft, model=resolved_study)
    if accepted_study is None or resolved_study is None:
        raise WorkflowError("falsification trials require an accepted study reference")
    mandate = derive_launch_mandate(
        getattr(workflow, "execution_store", None),
        workspace_id=request.workspace_id,
        execution_id=request.execution_id,
    )
    if mandate is not None:
        study_start, study_end = study_window(resolved_study.harness_configuration)
        violation = mandate.window_violation(
            rfc3339_ns(study_start), rfc3339_ns(study_end)
        )
        if violation is not None:
            raise WorkflowError(violation)
    document = build_hypothesis(workflow, request, draft)
    services_of(workflow).store.save(document, workspace_id=request.workspace_id)
    plan = _plan_from_document(document)
    staged = replace(state, draft=draft, hypothesis=document, plan=plan)
    final = run_trials(workflow, request, staged)
    report = falsification_report_artifact(workflow, final)
    evidence = _research_evidence(accepted_study, final, report)
    contract: Mapping[str, object] = {
        "sketchReference": sketch_reference.document(),
        "assumptionTests": {
            key: list(value) for key, value in draft.assumption_tests.items()
        },
        "trials": [dict(trial.as_dict()) for trial in final.trials],
        "research": evidence.as_dict(),
        "target": _VERDICT.value,
    }
    return contract, final, report


def _research_evidence(
    study: StudyProgramReference,
    state: HypothesisDevelopmentState,
    report: Mapping[str, object],
) -> ResearchEvidence:
    """Build the evidence envelope from the run outputs and the accepted plan.

    Every field is taken from the run result, the accepted study reference, the
    resolved hypothesis and plan, or the canonical report payload; nothing is
    inferred or fabricated, and the report digest covers exactly the canonical
    report JSON that the phase emits.
    """
    plan = state.plan
    draft = state.draft
    if plan is None or draft is None:
        raise WorkflowError("trials evidence requires the resolved hypothesis plan")
    falsifiers = {falsifier.falsifier_id: falsifier for falsifier in draft.falsifiers}
    assumptions: list[AssumptionEvidence] = []
    for trial in state.trials:
        falsifier = falsifiers.get(trial.falsifier_id)
        if falsifier is None:
            raise WorkflowError(f"trial {trial.test_id!r} names an unknown falsifier")
        assumptions.append(
            AssumptionEvidence(
                falsifier_id=trial.falsifier_id,
                statement=falsifier.test,
                role=falsifier.role.value,
                observable=falsifier.observable,
                condition=falsifier.condition,
                threshold=falsifier.threshold,
                test=trial.test_id,
                observed=trial.observed,
                falsified=trial.falsified,
                run_state=trial.run_state,
                detail=trial.detail,
            )
        )
    limitations = report.get("limitations")
    return ResearchEvidence(
        hypothesis_id=(
            None if state.hypothesis is None else state.hypothesis.hypothesis_id
        ),
        study=study,
        model=state.research_model_reference,
        workspace_artifact=state.research_model_artifact,
        fit=state.fitted_model_reference,
        fitted_parameters=state.fitted_parameters,
        runtime_configuration=state.research_model_runtime_configuration,
        plan=plan,
        assumptions=tuple(assumptions),
        controls=tuple(
            ControlEvidence(
                control_id=control.control_id,
                description=control.description,
                expectation=control.expectation,
            )
            for control in plan.controls
        ),
        report_digest=_digest(dict(report)),
        limitations=limitations if isinstance(limitations, str) else None,
    )


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
            universe=_compose_universe(payload.get("universe")),
            falsifiers=_compose_falsifiers(payload.get("falsifiers")),
            model=model,
            sizing=None,
            body=_optional_text(payload.get("body")),
            hypothesis_id=None,
            sketch_reference=(
                StrategySketchReference.parse(
                    cast(Mapping[str, object], payload["sketchReference"])
                )
                if isinstance(payload.get("sketchReference"), Mapping)
                else None
            ),
            assumption_tests={
                str(key): tuple(value)
                for key, value in cast(
                    Mapping[str, list[str]], payload.get("assumptionTests", {})
                ).items()
            },
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
    # One execution identity per falsification run: the persisted trial
    # reference is reused on resume; a fresh job is allocated on first run.
    if not isinstance(state.draft.model, ResearchModel):
        raise WorkflowError("trials require a resolved committed study")
    persisted_job_id = next(
        (trial.job_id for trial in state.trials if trial.job_id), None
    )
    pending = [test for test in plan.tests if test.test_id not in retained]
    if pending:
        if persisted_job_id is None:
            persisted_job_id = _recover_submission_job_id(workflow, request, state)
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
            job_id=persisted_job_id,
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
                job_id=result.job_id,
                digest=_digest(result.as_dict()),
                run_state=result.state,
                observed=observed,
                falsified=(
                    falsifier.falsifies(observed) if observed is not None else False
                ),
                detail=result.error,
            )
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


def _recover_submission_job_id(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> str | None:
    """Recover the execution's tracked submission for the current model revision.

    An interrupt after harness acceptance leaves the submission only in the
    execution's durable job links.  The tracked job whose attribution equals
    the current model revision is the same execution and is rejoined; a stale
    job from another revision does not match, so a fresh id is allocated
    instead of replaying an outdated submission.
    """
    chat = workflow.chat
    model = state.draft.model if state.draft is not None else None
    if chat is None or not isinstance(model, ResearchModel):
        return None
    tracked = chat.find_execution_tool_job(
        request.workspace_id,
        request.execution_id,
        tool_name=HARNESS_RESEARCH_MODEL_TOOL,
        attribution=research_model_attribution(model),
    )
    return None if tracked is None else tracked.job_id
