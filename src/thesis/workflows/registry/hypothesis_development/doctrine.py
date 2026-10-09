"""The hypothesis-development doctrine: the data the general engine reads.

This module is the single place where the concrete workflow describes itself to
:class:`~thesis.workflows.core.workflow.Workflow`: phase metadata, the trusted
context and outcome projections, the state reconstruction path, and the
model/persistence adapters.  No workflow class exists here; a workflow is the
general object plus this data.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from thesis.experiments.mandate import (
    declared_launch_partition,
    derive_launch_mandate,
    rfc3339,
)
from thesis.workflows.core.spec import WorkflowContext
from thesis.workflows.core.workflow import Workflow, WorkflowDoctrine

from .artifacts import (
    discovery_report_artifact,
    forward_report_artifact,
    variation_report_artifact,
)
from .contracts.creation import HypothesisDevelopmentRequest
from .design import frozen_design
from .errors import (
    HypothesisDevelopmentIntakeError,
    WorkflowError,
)
from .exposure import submitted_exposures
from .phases import (
    _DISCOVERY,
    _FORWARD_VALIDATION,
    _MODEL_CREATION,
    _NULL_TESTING,
    _OPTIMIZATION,
    _STRATEGY_BACKTESTING,
    _STRATEGY_DESIGN,
    _STRATEGY_RCA,
    _VARIATION,
    _VERDICT,
    phase_from_value,
    workflow_definition,
)
from .primitives import (
    _WORKFLOW_ACTOR,
    _text,
)
from .rebuild import fold_transition, rebuild_state
from .services import HypothesisDevelopmentServices
from .state import HypothesisDevelopmentOutcome, HypothesisDevelopmentState

_DEFAULT_ACTIVITY = "Preparing the next workflow step."


def hypothesis_development_doctrine(
    services: HypothesisDevelopmentServices,
) -> WorkflowDoctrine[
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
    HypothesisDevelopmentOutcome,
]:
    """Assemble the doctrine the general workflow object reads."""
    from thesis.workflows.core.phase import ConversationPhase

    from .spec import phase_declarations

    declarations = phase_declarations()
    agents = tuple(item for item in declarations if item.orchestrator is not None)
    return WorkflowDoctrine(
        definition=workflow_definition(),
        initial_phase=_DISCOVERY.value,
        actor=_WORKFLOW_ACTOR,
        active_phases=tuple(item.spec.phase for item in agents),
        conversational_phases=frozenset(
            item.spec.phase
            for item in agents
            if isinstance(item.spec, ConversationPhase)
        ),
        orchestrators={
            item.spec.phase: item.orchestrator
            for item in agents
            if item.orchestrator is not None
        },
        tasks={item.spec.phase: item.task for item in agents if item.task is not None},
        activity={
            item.spec.phase: item.activity
            for item in agents
            if item.activity is not None
        },
        default_activity=_DEFAULT_ACTIVITY,
        mcp_tools=services.mcp_tools,
        phase_mcp_tools=services.phase_mcp_tools,
        phase_tools=services.phase_tools,
        request_of=_request_of,
        context_of=_context_of,
        project=_project,
        reconstruct=_reconstruct_state,
        summarize=_phase_summary,
        report=_phase_report,
        validate_launch=_validate_launch,
        reduce_transition=fold_transition,
    )


# -- launch and trusted context ---------------------------------------------


def _request_of(
    execution: Any, ideas: str, launch_options: Mapping[str, object]
) -> HypothesisDevelopmentRequest:
    """Bind one durable execution and the supplied idea into a request."""
    return HypothesisDevelopmentRequest(
        workspace_id=execution.workspace_id,
        chat_id=execution.chat_id,
        execution_id=execution.execution_id,
        trigger_message_id=execution.trigger_message_id,
        requested_by=execution.requested_by,
        ideas=ideas,
        partition=declared_launch_partition(launch_options.get("partition")),
        dependencies=dict(cast(Mapping[str, object], launch_options["partition"])),
        intent_epoch=execution.intent_epoch,
        created_at=execution.created_at,
    )


def _context_of(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        HypothesisDevelopmentOutcome,
    ],
    request: HypothesisDevelopmentRequest,
) -> WorkflowContext:
    """Return the trusted engine context for one execution turn."""
    return WorkflowContext(
        workspace_id=request.workspace_id,
        chat_id=request.chat_id,
        actor_id=request.requested_by,
        execution_id=request.execution_id,
        trigger_message_id=request.trigger_message_id,
        intent_epoch=request.intent_epoch,
        delivery=workflow.delivery,
    )


def _validate_launch(
    ideas: str,
    trigger_message_id: str | None,
    anchor_sub_agent_id: str | None,
    launch_options: Mapping[str, object],
) -> None:
    """Reject an invalid intake before a durable execution exists."""
    if set(launch_options) != {"partition"}:
        raise HypothesisDevelopmentIntakeError(
            "launch_options must contain the explicit partition declaration"
        )
    declared_launch_partition(launch_options.get("partition"))
    try:
        _text(ideas, "ideas", max_length=8_192)
    except WorkflowError as error:
        raise HypothesisDevelopmentIntakeError(str(error)) from error
    try:
        _text(trigger_message_id, "trigger_message_id", max_length=256)
    except WorkflowError as error:
        raise HypothesisDevelopmentIntakeError(str(error)) from error
    if anchor_sub_agent_id is not None:
        _text(anchor_sub_agent_id, "anchor_sub_agent_id", max_length=256)


# -- state reconstruction ----------------------------------------------------


def _reconstruct_state(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        HypothesisDevelopmentOutcome,
    ],
    request: HypothesisDevelopmentRequest,
) -> HypothesisDevelopmentState:
    """Rebuild the run state from the execution store (REQ-B4).

    The store document is authoritative for machine state: the identity and
    launch request were written at launch and every transition was committed
    with its completion contract, so folding the stored transitions restores
    the full typed state — draft, trials, verdict — without reading the chat
    event log or the hypothesis side-store.
    """
    store = workflow.execution_store
    if store is None:
        raise WorkflowError("workflow requires the execution store")
    document = store.get(request.execution_id)
    return rebuild_state(
        request.workspace_id,
        request.execution_id,
        document.transitions,
        initial_phase=_DISCOVERY,
        pipes=workflow.spec.piping,
    )


# -- phase projections -------------------------------------------------------


def _phase_value(phase: object) -> str:
    value = getattr(phase, "value", None)
    if isinstance(value, str):
        return value
    return str(phase)


def _phase_summary(state: HypothesisDevelopmentState, phase: object) -> str:
    """Summarize what one phase produced, for its transcript."""
    resolved = phase_from_value(_phase_value(phase))
    if resolved is _DISCOVERY:
        if state.discovery is not None:
            candidate = state.discovery.sketch_reference
            if candidate is not None:
                return f"Discovery sketch: {candidate.sketch_id} ({candidate.version[:12]})"
            return (
                "Discovery concluded no candidate: "
                f"{state.discovery.no_candidate_reason}"
            )
        return "Discovery completed without a recorded candidate."
    if resolved is _MODEL_CREATION:
        if state.hypothesis is not None:
            return f"Hypothesis: {state.hypothesis.front_matter.claim}"
        if state.draft is not None:
            return f"Drafted hypothesis: {state.draft.claim}"
    elif resolved is _VERDICT and state.verdict is not None:
        return f"Verdict: {state.verdict.outcome}. {state.verdict.reason}".strip()
    elif resolved is _STRATEGY_DESIGN and state.strategy is not None:
        reference = state.strategy
        return (
            f"Designed strategy {reference['programId']} {reference['version']} "
            f"(commit {str(reference['gitCommit'])[:12]})."
        )
    elif resolved is _STRATEGY_BACKTESTING and state.strategy_backtest is not None:
        return (
            f"Strategy backtest {state.strategy_backtest.job_id} ended in "
            f"state {state.strategy_backtest.state}."
        )
    elif resolved is _STRATEGY_RCA:
        return f"Recorded {len(state.rca)} strategy RCA diagnosis(es)."
    elif resolved is _OPTIMIZATION and state.candidate_evaluation is not None:
        evaluation = state.candidate_evaluation
        return (
            f"Candidate evaluation {evaluation.batch_id} selected "
            f"{len(evaluation.shortlist)} of {len(evaluation.assignments)} "
            "candidate(s) for the shortlist."
        )
    elif resolved is _NULL_TESTING and state.null_testing is not None:
        evaluation = state.null_testing
        succeeded = sum(
            1 for replicate in evaluation.replicates if replicate.state == "succeeded"
        )
        return (
            f"Null test {evaluation.batch_id} measured {evaluation.statistic} "
            f"p={evaluation.p_value:.4g} from {succeeded} succeeded replicate(s)."
        )
    elif resolved is _VARIATION and state.variation is not None:
        evaluation = state.variation
        return (
            f"Variation {evaluation.batch_id} varied "
            f"{len(evaluation.parameters)} parameter(s) across "
            f"{len(evaluation.points)} point(s)."
        )
    elif resolved is _FORWARD_VALIDATION and state.forward_validation is not None:
        evaluation = state.forward_validation
        return (
            f"Forward validation measured {evaluation.statistic} "
            f"p={evaluation.null.p_value:.4g} on "
            f"[{evaluation.window.start}, {evaluation.window.end})."
        )
    return f"{resolved.value.capitalize()} phase completed."


def _phase_report(
    workflow: Workflow[Any, Any, Any],
    state: HypothesisDevelopmentState,
    active: object,
) -> Mapping[str, object] | None:
    """Return the rich structured report one phase exit projects, if any.

    Discovery projects its selected candidate as the discovery artifact and
    variation projects its parameter neighborhood as the variation artifact;
    every other phase's report stays empty (model creation's hypothesis result
    is the durable hypothesis document, not a phase report).
    """
    if active is None:
        return None
    phase = getattr(active, "phase", None)
    if phase is None:
        return None
    resolved = phase_from_value(_phase_value(phase))
    if resolved is _FORWARD_VALIDATION:
        report = forward_report_artifact(state)
        store = workflow.execution_store
        if report is None or store is None:
            return report
        document = store.get(state.execution_id)
        mandate = derive_launch_mandate(
            store, workspace_id=state.workspace_id, execution_id=state.execution_id
        )
        if mandate is None:
            raise WorkflowError("forward report requires its frozen partition")
        design = frozen_design(
            state,
            rfc3339(mandate.forward_window.start_ns),
            rfc3339(mandate.forward_window.end_ns),
        )
        exposures = submitted_exposures(workflow, state.execution_id)
        prior_designs = {
            str(item.get("designSha256"))
            for item in exposures
            if item.get("phase") == "forward_validation"
            and item.get("designSha256") != design.sha256()
        }
        report.update(
            {
                "frozenDesign": design.as_dict(),
                "launchDesign": dict(document.launch_request),
                "submissionExposures": list(exposures),
                "priorForwardDesignCount": len(prior_designs),
                "forwardEvidenceScope": "adaptive_holdout_reuse"
                if prior_designs
                else "frozen_design_holdout",
            }
        )
        return report
    if resolved is _VARIATION:
        if state.variation is None:
            return None
        return variation_report_artifact(state)
    if resolved is not _DISCOVERY:
        return None
    if state.discovery is None:
        return None
    return discovery_report_artifact(state)


def _project(
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentOutcome:
    """Project one run state into its detached outcome."""
    return HypothesisDevelopmentOutcome(
        workspace_id=state.workspace_id,
        execution_id=state.execution_id,
        phase=state.phase,
        fenced=state.fenced,
        transitions=state.transitions,
        discovery=state.discovery,
        hypothesis=state.hypothesis,
        research_model_reference=state.research_model_reference,
        research_model_artifact=state.research_model_artifact,
        fitted_model_reference=state.fitted_model_reference,
        fitted_parameters=state.fitted_parameters,
        research_model_runtime_configuration=state.research_model_runtime_configuration,
        research_evidence=state.research_evidence,
        plan=state.plan,
        trials=state.trials,
        rca=state.rca,
        verdict=state.verdict,
        strategy=state.strategy,
        hypothesis_cycles=state.hypothesis_cycles,
        strategy_backtest=state.strategy_backtest,
        candidate_evaluation=state.candidate_evaluation,
        null_testing=state.null_testing,
        variation=state.variation,
        forward_validation=state.forward_validation,
        detail=state.detail,
    )


# -- model adapters ----------------------------------------------------------


__all__ = [
    "hypothesis_development_doctrine",
]
