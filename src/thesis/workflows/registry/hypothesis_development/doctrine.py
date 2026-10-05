"""The hypothesis-development doctrine: the data the general engine reads.

This module is the single place where the concrete workflow describes itself to
:class:`~thesis.workflows.core.workflow.Workflow`: phase metadata, the trusted
context and outcome projections, the state reconstruction path, and the
model/persistence adapters.  No workflow class exists here; a workflow is the
general object plus this data.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from thesis.research.hypothesis import (
    HypothesisArtifactError,
    HypothesisDocument,
)
from thesis.workflows.core.spec import WorkflowContext
from thesis.workflows.core.workflow import Workflow, WorkflowDoctrine

from .artifacts import discovery_report_artifact
from .contracts.creation import HypothesisDevelopmentRequest
from .errors import (
    HypothesisDevelopmentIntakeError,
    WorkflowError,
)
from .phases import (
    _DISCOVERY,
    _MODEL_CREATION,
    _OPTIMIZATION,
    _STRATEGY_BACKTESTING,
    _STRATEGY_DESIGN,
    _STRATEGY_RCA,
    _VERDICT,
    phase_from_value,
    workflow_definition,
)
from .primitives import (
    _WORKFLOW_ACTOR,
    _text,
)
from .rebuild import fold_transition, rebuild_state
from .services import HypothesisDevelopmentServices, services_of
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


def _request_of(execution: Any, ideas: str) -> HypothesisDevelopmentRequest:
    """Bind one durable execution and the supplied idea into a request."""
    return HypothesisDevelopmentRequest(
        workspace_id=execution.workspace_id,
        chat_id=execution.chat_id,
        execution_id=execution.execution_id,
        trigger_message_id=execution.trigger_message_id,
        requested_by=execution.requested_by,
        ideas=ideas,
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
) -> None:
    """Reject an invalid intake before a durable execution exists."""
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


def _load_hypothesis(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
) -> HypothesisDocument | None:
    """Load the execution's hypothesis artifact when it was finalized."""
    try:
        return services_of(workflow).store.load(
            f"h-{request.execution_id}", workspace_id=request.workspace_id
        )
    except HypothesisArtifactError:
        return None


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
            return f"Discovery candidate: {state.discovery.candidate.opportunity}"
        return "Discovery completed without a recorded candidate."
    if resolved is _MODEL_CREATION:
        if state.hypothesis is not None:
            return f"Hypothesis: {state.hypothesis.front_matter.claim}"
        if state.draft is not None:
            return f"Drafted hypothesis: {state.draft.claim}"
    elif resolved is _VERDICT and state.verdict is not None:
        return f"Verdict: {state.verdict.outcome}. {state.verdict.reason}".strip()
    elif resolved is _STRATEGY_DESIGN and state.strategy_bundle is not None:
        bundle = state.strategy_bundle
        return (
            f"Designed strategy {bundle.strategy_id} {bundle.version} "
            f"(sha256 {bundle.bundle_sha256[:12]}â€¦)."
        )
    elif resolved is _STRATEGY_BACKTESTING and state.strategy_backtest is not None:
        return (
            f"Strategy backtest {state.strategy_backtest.job_id} ended in "
            f"state {state.strategy_backtest.state}."
        )
    elif resolved is _STRATEGY_RCA:
        return f"Recorded {len(state.rca)} strategy RCA diagnosis(es)."
    elif resolved is _OPTIMIZATION and state.optimization_backtest is not None:
        return "Optimization backtest completed."
    return f"{resolved.value.capitalize()} phase completed."


def _phase_report(
    workflow: Workflow[Any, Any, Any],
    state: HypothesisDevelopmentState,
    active: object,
) -> Mapping[str, object] | None:
    """Return the rich structured report one phase exit projects, if any.

    Discovery projects its selected candidate as the discovery artifact; every
    other phase's report stays empty (model creation's hypothesis result is the
    durable hypothesis document, not a phase report).
    """
    if active is None:
        return None
    phase = getattr(active, "phase", None)
    if phase is None:
        return None
    resolved = phase_from_value(_phase_value(phase))
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
        plan=state.plan,
        trials=state.trials,
        rca=state.rca,
        verdict=state.verdict,
        strategy_bundle=state.strategy_bundle,
        exit_criteria=state.exit_criteria,
        hypothesis_cycles=state.hypothesis_cycles,
        strategy_cycles=state.strategy_cycles,
        strategy_backtest=state.strategy_backtest,
        optimization_backtest=state.optimization_backtest,
        journal_id=state.journal_id,
        detail=state.detail,
    )


# -- model adapters ----------------------------------------------------------


__all__ = [
    "hypothesis_development_doctrine",
]
