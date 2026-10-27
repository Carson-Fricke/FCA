"""Assemble the hypothesis-development workflow specification and object.

The spec is the registered definition of the concrete workflow: its ordered
phases, the allowed transition table, and the typed piping between phases.
Every phase is declared as a sealed-union phase object — conversation,
one-shot, or deterministic — bound to the module that owns its behavior; the
state machine wiring lives here instead of inside a workflow class.  The
builder then assembles the one general
:class:`~thesis_runtime.workflows.workflow.Workflow` instance from that spec, the
doctrine, the services bundle, and the ports.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from types import MappingProxyType
from typing import Any, Literal, cast

from thesis.experiments.mandate import (
    ResearchMandate,
    mandate_section,
    resolve_research_mandate,
)
from thesis.research.hypothesis import HypothesisStore, InMemoryHypothesisStore
from thesis.workflows.backtests import BacktestRunner
from thesis.workflows.research_models import (
    PROGRAM_REFERENCE_SCHEMA,
    BacktestResearchModelRunner,
    ProgramSnapshots,
    ResearchModelRunner,
)
from thesis.workspaces.sketch_contract import StrategySketchReference
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.execution.worker import WorkStore
from thesis_runtime.tools.schema import render_violations, validate_arguments
from thesis_runtime.workflows.builder import WorkflowBuilder
from thesis_runtime.workflows.execution_store import ExecutionStore
from thesis_runtime.workflows.spec import HandoffContext, WorkflowSpec
from thesis_runtime.workflows.workflow import Detach, Workflow, WorkflowPorts

from .activities import DevelopmentActivities
from .callbacks import (
    ConversationPhase,
    DeterministicPhase,
    OneShotPhase,
    PhaseSpec,
    PipingFunction,
    TerminalPhase,
)
from .contracts.candidates import (
    CANDIDATE_EVALUATION_PAYLOAD_SCHEMA,
    CandidateEvaluation,
    candidate_evaluation_payload_errors,
)
from .contracts.creation import HypothesisDevelopmentRequest
from .contracts.discovery import DiscoveryDraft
from .contracts.evidence import ResearchEvidence
from .contracts.forward import (
    FORWARD_PAYLOAD_SCHEMA,
    ForwardEvaluation,
    forward_payload_errors,
)
from .contracts.nulls import (
    NULL_TEST_EVALUATION_PAYLOAD_SCHEMA,
    NullTestEvaluation,
    null_test_evaluation_payload_errors,
)
from .contracts.rca import RcaDiagnosis
from .contracts.strategy import (
    StrategyBacktest,
    strategy_backtest_payload_errors,
)
from .contracts.validation import (
    DISCOVERY_COMPLETION_SCHEMA,
    HYPOTHESIS_COMPLETION_SCHEMA,
    VERDICT_PAYLOAD_SCHEMA,
    discovery_payload_errors,
    format_contract_errors,
    hypothesis_payload_errors,
    verdict_payload_errors,
)
from .contracts.variation import (
    VARIATION_PAYLOAD_SCHEMA,
    VariationEvaluation,
    variation_payload_errors,
)
from .contracts.verdict import FalsificationVerdict
from .errors import FalsificationComposeError, WorkflowError
from .phases import (
    _FAILED,
    _VERDICT,
    HypothesisDevelopmentPhase,
)
from .phases.trials import _draft_from_contract, run_falsification_trials
from .piping import (
    identity_pipe,
    reset_backtests,
    reset_discovery,
    reset_premise,
)
from .primitives import (
    _RCA_SCHEMA,
    _new_id,
    _utc_now,
)
from .rebuild import (
    PayloadFold,
    _fold_discovery,
    _fold_draft,
    _fold_forward_validation,
    _fold_null_testing,
    _fold_optimization,
    _fold_strategy_backtesting,
    _fold_strategy_design,
    _fold_strategy_rca,
    _fold_trials,
    _fold_variation,
    _fold_verdict,
)
from .services import (
    HypothesisDevelopmentServices,
    services_of,
)
from .state import HypothesisDevelopmentOutcome, HypothesisDevelopmentState

__all__ = [
    "build_hypothesis_development_spec",
    "build_hypothesis_development_workflow",
    "piping",
]


def _start_handoff(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
    context: HandoffContext,
) -> str | None:
    """Create the first phase's prompt from the operator's launching message.

    The workflow starts in discovery, so the start adapter builds the discovery
    prompt around the caller's message, verbatim.  A non-start handoff returns
    ``None`` so each later phase keeps its declared seed.
    """
    if context.source is not None:
        return None
    return _discovery_prompt(workflow, request)


def _mandate(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
) -> ResearchMandate | None:
    """Read the execution's discovery-owned research windows."""
    return resolve_research_mandate(
        workflow.execution_store,
        workspace_id=request.workspace_id,
        execution_id=request.execution_id,
    )


def _discovery_prompt(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
) -> str:
    """Build the discovery prompt: phase instructions plus the operator's words."""
    from .phases.creation import trigger_text
    from .prompts.discovery import _discovery_turn_prompt

    return (
        f"Launch timestamp (UTC): {request.created_at.isoformat() if request.created_at else 'unavailable'}\n\n"
        + _discovery_turn_prompt(
            [("user", trigger_text(workflow, request))],
            mandate=_mandate(workflow, request),
        )
    )


def _discovery_seed(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the discovery agent with its instructions or a restart brief.

    The workflow's first entry is a fresh start: the full discovery prompt
    carries the operator's launching message.  A verdict restart re-enters the
    same durable conversation, so its brief carries only the verdict that
    superseded the previous candidate.
    """
    del state
    if context is None or context.source is None:
        return _discovery_prompt(workflow, request)
    from .prompts.discovery import _discovery_restart_section

    return _discovery_restart_section(
        dict(context.completion) if context.completion else None
    )


def _sketch_section(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
    reference: StrategySketchReference | None,
) -> list[str]:
    """Resolve the immutable sketch, never today's edited workspace artifact."""
    if reference is None:
        return []
    programs = services_of(workflow).programs
    if programs is None:
        raise WorkflowError(
            "workspace programs are required to resolve the accepted sketch"
        )
    document = programs.verify_strategy_sketch(
        request.workspace_id, reference.document()
    )
    return [
        "",
        "Accepted strategy sketch (exact committed revision):",
        json.dumps(
            {"reference": reference.document(), "sketch": document},
            ensure_ascii=True,
            indent=2,
        ),
    ]


def _accepted_discovery(
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> DiscoveryDraft | None:
    """Return the selected candidate from state or the accepted handoff.

    The live pipe keeps an already-selected draft, but the first
    discovery→model-creation transition carries the candidate in the accepted
    completion rather than the pre-transition state; both paths seed the same
    handoff.
    """
    if state.discovery is not None:
        return state.discovery
    payload = context.completion.get("discovery") if context else None
    if not isinstance(payload, Mapping) or not payload:
        return None
    try:
        return DiscoveryDraft.from_dict(payload)
    except WorkflowError:
        return None


def _model_creation_seed(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the model-creation agent with its instructions and the candidate.

    A fresh entry hands over the full model-creation instructions; a verdict
    circle-back re-enters the same durable conversation with the compact
    revision brief that points back at the recorded trial findings.  Both
    entries carry the still-selected discovery candidate when the state or the
    accepted handoff holds one, because model creation must model exactly that
    candidate.
    """
    from .prompts.model_creation import (
        _model_creation_instructions,
        _model_creation_revision_section,
    )

    if context is not None and context.source == _VERDICT.value:
        report = _latest_phase_artifact(workflow, request, "falsification_trials")
        parts = [
            _model_creation_revision_section(
                dict(context.completion) if context.completion else None,
                report,
            )
        ]
    else:
        parts = [_model_creation_instructions()]
        if context is not None and context.completion:
            parts += [
                "",
                f"Accepted completion from the {context.source} phase:",
                json.dumps(
                    dict(context.completion),
                    ensure_ascii=True,
                    indent=2,
                    default=str,
                ),
            ]
    mandate = _mandate(workflow, request)
    if mandate is not None:
        parts += ["", mandate_section(mandate)]
    discovery = _accepted_discovery(state, context)
    parts += _sketch_section(
        workflow, request, None if discovery is None else discovery.sketch_reference
    )
    return "\n".join(parts)


def _latest_phase_artifact(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
    phase: str,
) -> Mapping[str, object] | None:
    """Return the latest recorded artifact for one phase, if any.

    The recorded report outlives the state reset a loop-back applies, so the
    revision seed reads it from the durable execution events instead of the
    cleared typed state.
    """
    from thesis_runtime.execution.events import ExecutionEventType

    for event in reversed(
        workflow.observe(request.workspace_id, request.execution_id).events
    ):
        if event.event_type is not ExecutionEventType.WORKFLOW_PHASE_RESULT:
            continue
        payload = event.payload
        if payload.get("phase") != phase:
            continue
        artifact = payload.get("artifact")
        if isinstance(artifact, Mapping):
            return dict(artifact)
    return None


def _strategy_design_seed(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the design agent with the surviving hypothesis and its ask protocol.

    The agent must explain the strategy it designs in prose and ask the
    operator to confirm it before the workflow advances to backtesting.
    """
    from .prompts.strategy_design import _strategy_design_instructions

    parts = [_strategy_design_instructions()]
    reference = None if state.verdict is None else state.verdict.sketch_reference
    if reference is None and context.source == "verdict":
        verdict = context.completion.get("verdict")
        if isinstance(verdict, Mapping) and isinstance(
            verdict.get("sketchReference"), Mapping
        ):
            reference = StrategySketchReference.parse(verdict["sketchReference"])
    if reference is None and state.discovery is not None:
        reference = state.discovery.sketch_reference
    parts += _sketch_section(workflow, request, reference)
    hypothesis = _surviving_hypothesis(state)
    if hypothesis is not None:
        parts += [
            "",
            "Surviving hypothesis:",
            json.dumps(hypothesis, ensure_ascii=True, indent=2, default=str),
        ]
    if state.research_evidence is not None:
        parts += [
            "",
            "Research evidence:",
            json.dumps(
                state.research_evidence.as_dict(),
                ensure_ascii=True,
                indent=2,
                default=str,
            ),
        ]
    if state.discovery is not None:
        parts += [
            "",
            "Discovery:",
            json.dumps(
                state.discovery.as_dict(),
                ensure_ascii=True,
                indent=2,
                default=str,
            ),
        ]
    if context is not None and context.completion:
        parts += [
            "",
            f"Accepted completion from the {context.source} phase:",
            json.dumps(
                dict(context.completion), ensure_ascii=True, indent=2, default=str
            ),
        ]
    return "\n".join(parts)


def _surviving_hypothesis(
    state: HypothesisDevelopmentState,
) -> dict[str, object] | None:
    """Return the claim and falsifiers the strategy must serve, if recorded."""
    draft = state.draft
    if draft is None:
        return None
    return {
        "claim": draft.claim,
        "disagreement": draft.disagreement,
        "riskFactor": draft.risk_factor,
        "horizon": draft.horizon,
        "universe": list(draft.universe),
        "falsifiers": [
            {
                "falsifierId": falsifier.falsifier_id,
                "observable": falsifier.observable,
                "condition": falsifier.condition,
                "threshold": falsifier.threshold,
                "test": falsifier.test,
            }
            for falsifier in draft.falsifiers
        ],
    }


def _optimization_seed(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the evaluation agent with its instructions and the accepted handoff.

    The strategy receipt and the backtest evidence arrive in the accepted
    completion from the preceding phase, so the seed carries the candidate
    evaluation instructions plus that handoff verbatim.
    """
    del workflow, request, state
    from .prompts.candidates import _candidates_instructions

    parts = [_candidates_instructions()]
    if context is not None and context.completion:
        parts += [
            "",
            f"Accepted completion from the {context.source} phase:",
            json.dumps(
                dict(context.completion), ensure_ascii=True, indent=2, default=str
            ),
        ]
    return "\n".join(parts)


def _null_testing_seed(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the null-testing agent with its instructions and the evaluation.

    The candidate evaluation arrives as the accepted completion from the
    optimization phase; the seed carries the null-testing instructions plus
    that handoff verbatim so the agent can reuse the shortlisted candidate's
    parameters and observed metrics.
    """
    del workflow, request, state
    from .prompts.nulls import _nulls_instructions

    parts = [_nulls_instructions()]
    if context is not None and context.completion:
        parts += [
            "",
            f"Accepted completion from the {context.source} phase:",
            json.dumps(
                dict(context.completion), ensure_ascii=True, indent=2, default=str
            ),
        ]
    return "\n".join(parts)


def _variation_seed(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the variation agent with its instructions and the evaluation.

    The null-test acceptance arrives as the accepted completion from the
    null-testing phase; the still-recorded candidate evaluation supplies the
    shortlist the center is chosen from, so the seed carries both plus the
    variation instructions.
    """
    del workflow, request
    from .prompts.variation import _variation_instructions

    parts = [_variation_instructions()]
    evaluation = getattr(state, "candidate_evaluation", None)
    if evaluation is not None:
        parts += [
            "",
            "Recorded candidate evaluation:",
            json.dumps(
                evaluation.as_dict(),
                ensure_ascii=True,
                indent=2,
                default=str,
            ),
        ]
    if context is not None and context.completion:
        parts += [
            "",
            f"Accepted completion from the {context.source} phase:",
            json.dumps(
                dict(context.completion), ensure_ascii=True, indent=2, default=str
            ),
        ]
    return "\n".join(parts)


def _forward_validation_seed(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the forward-validation agent with both frozen designs.

    The variation acceptance arrives as the accepted completion from the
    variation phase and the null-test acceptance remains in the typed state, so
    the seed carries the forward instructions plus the recorded null test and
    variation the agent must re-run unchanged on the protected forward window.
    """
    del workflow, request
    from .prompts.forward import _forward_instructions

    parts = [_forward_instructions()]
    null_testing = getattr(state, "null_testing", None)
    if null_testing is not None:
        parts += [
            "",
            "Recorded null test:",
            json.dumps(
                null_testing.as_dict(),
                ensure_ascii=True,
                indent=2,
                default=str,
            ),
        ]
    variation = getattr(state, "variation", None)
    if variation is not None:
        parts += [
            "",
            "Recorded variation:",
            json.dumps(
                variation.as_dict(),
                ensure_ascii=True,
                indent=2,
                default=str,
            ),
        ]
    if context is not None and context.completion:
        parts += [
            "",
            f"Accepted completion from the {context.source} phase:",
            json.dumps(
                dict(context.completion), ensure_ascii=True, indent=2, default=str
            ),
        ]
    return "\n".join(parts)


def _verdict_seed(
    workflow: DevelopmentActivities,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the verdict agent with the ask protocol and the recorded trials.

    The deterministic trials land as the accepted completion from the
    falsification-trials phase; the verdict agent proposes the verdict from
    them and must ask the operator through ``request_workflow_input`` before
    completing — it never auto-completes over the trials.
    """
    from .prompts.verdict import _verdict_instructions

    parts = [_verdict_instructions()]
    parts += _sketch_section(
        workflow,
        request,
        None if state.discovery is None else state.discovery.sketch_reference,
    )
    if context.source is not None and context.completion:
        parts += [
            "",
            f"Accepted completion from the {context.source} phase:",
            json.dumps(
                dict(context.completion), ensure_ascii=True, indent=2, default=str
            ),
        ]
    return "\n".join(parts)


def _contract_seed(task: str, *, schema: str | None = None) -> Callable[..., str]:
    """Build a seed that carries the accepted handoff and the contract shape."""

    def seed(
        workflow: DevelopmentActivities,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        context: HandoffContext,
    ) -> str:
        del workflow, request, state
        parts = [task]
        if context.source is not None and context.completion:
            parts += [
                "",
                f"Accepted completion from the {context.source} phase:",
                json.dumps(
                    dict(context.completion), ensure_ascii=True, indent=2, default=str
                ),
            ]
        if schema is not None:
            parts += ["", f"Required shape: {schema}"]
        return "\n".join(parts)

    return seed


def _discovery_validator(completion: Mapping[str, object]) -> str | None:
    """Deeply validate the discovery contract before any transition commits.

    Every missing or wrongly-typed candidate or attempt field is reported at
    once, so the agent repairs the whole contract in one turn, and the typed
    :class:`DiscoveryDraft` decoder re-runs as a drift guard: anything the
    collector missed still rejects the contract here rather than later in the
    rebuild fold.  The target must match the branch: a selected candidate
    advances model creation, a recorded no-candidate reason finishes the run.
    """
    payload = completion.get("discovery")
    if not isinstance(payload, Mapping) or not payload:
        return "completion field 'discovery' must be a non-empty object"
    errors = discovery_payload_errors(payload)
    if not errors:
        try:
            DiscoveryDraft.from_dict(payload)
        except WorkflowError as error:
            errors = [str(error)]
    target = completion.get("target")
    has_candidate = payload.get("sketchReference") is not None
    expected = "model_creation" if has_candidate else "completed"
    if isinstance(target, str) and target not in {expected, _FAILED.value}:
        errors.append(
            "discovery with "
            f"{'a candidate must target model_creation' if has_candidate else 'no candidate must target completed'}"
            f", got {target!r}"
        )
    if errors:
        return format_contract_errors(errors)
    return None


def _trials_validator(completion: Mapping[str, object]) -> str | None:
    """Validate the deterministic trials contract and its evidence envelope."""
    raw = completion.get("trials")
    if isinstance(raw, str) or not isinstance(raw, Sequence) or not raw:
        return "completion field 'trials' must be a non-empty list"
    research = completion.get("research")
    if not isinstance(research, Mapping) or not research:
        return "completion field 'research' must be a non-empty object"
    try:
        ResearchEvidence.from_dict(research)
    except WorkflowError as error:
        return f"completion field 'research' is invalid: {error}"
    return None


def _verdict_validator(completion: Mapping[str, object]) -> str | None:
    """Deeply validate the verdict contract before any transition commits.

    The advertised verdict schema is collected in full and the typed
    :class:`FalsificationVerdict` decoder re-runs as a drift guard: a malformed
    outcome, reason, or trial list rejects the contract here instead of
    poisoning the rebuild fold.  The engine validates the declared edge only;
    outcome-to-target consistency stays the agent's proposal.
    """
    payload = completion.get("verdict")
    if not isinstance(payload, Mapping) or not payload:
        return "completion field 'verdict' must be a non-empty object"
    errors = verdict_payload_errors(payload)
    if not errors:
        try:
            FalsificationVerdict.from_dict(payload)
        except WorkflowError as error:
            errors = [str(error)]
    if errors:
        return format_contract_errors(errors)
    return None


def _optimization_validator(completion: Mapping[str, object]) -> str | None:
    """Deeply validate the candidate-evaluation contract before any transition.

    The advertised candidate payload is collected in full and the typed
    :class:`CandidateEvaluation` decoder re-runs as a drift guard: a malformed
    assignment, a shortlist vector absent from the submissions, or a
    non-finite metric rejects the contract here instead of poisoning the
    rebuild fold.
    """
    payload = completion.get("optimization")
    if not isinstance(payload, Mapping) or not payload:
        return "completion field 'optimization' must be a non-empty object"
    errors = candidate_evaluation_payload_errors(payload)
    if not errors:
        try:
            CandidateEvaluation.from_dict(payload)
        except WorkflowError as error:
            errors = [str(error)]
    if errors:
        return format_contract_errors(errors)
    return None


def _null_testing_validator(completion: Mapping[str, object]) -> str | None:
    """Deeply validate the null-testing contract before any transition.

    The advertised null-test payload is collected in full and the typed
    :class:`NullTestEvaluation` decoder re-runs as a drift guard: an absent
    statistic, a mismatched observed value, an exceedance count or p-value
    that disagrees with the returned replicate metrics, a duplicate seed, or
    no succeeded replicate rejects the contract here instead of poisoning the
    rebuild fold.
    """
    payload = completion.get("nullTesting")
    if not isinstance(payload, Mapping) or not payload:
        return "completion field 'nullTesting' must be a non-empty object"
    errors = null_test_evaluation_payload_errors(payload)
    if not errors:
        try:
            NullTestEvaluation.from_dict(payload)
        except WorkflowError as error:
            errors = [str(error)]
    if errors:
        return format_contract_errors(errors)
    return None


def _variation_validator(completion: Mapping[str, object]) -> str | None:
    """Deeply validate the variation contract before any transition.

    The advertised variation payload is collected in full and the typed
    :class:`VariationEvaluation` decoder re-runs as a drift guard: a point that
    differs from the center in more than one parameter, an uncovered declared
    parameter, a repeated center or duplicate point vector, absent succeeded
    metrics, or a non-finite metric or parameter value rejects the contract
    here instead of poisoning the rebuild fold.
    """
    payload = completion.get("variation")
    if not isinstance(payload, Mapping) or not payload:
        return "completion field 'variation' must be a non-empty object"
    errors = variation_payload_errors(payload)
    if not errors:
        try:
            VariationEvaluation.from_dict(payload)
        except WorkflowError as error:
            errors = [str(error)]
    if errors:
        return format_contract_errors(errors)
    return None


def _forward_validator(completion: Mapping[str, object]) -> str | None:
    """Deeply validate the forward-validation contract before any transition.

    The advertised forward payload is collected in full and the typed
    :class:`ForwardEvaluation` decoder re-runs as a drift guard.  The nested
    null and variation evidence is checked through the null-testing and
    variation contracts' own cross-checks: an observed value that disagrees
    with its metrics, a missing succeeded replicate, a mismatched exceedance
    count or p-value, a point that differs in two parameters, an uncovered
    declared parameter, or an unparseable forward window rejects the contract
    here instead of poisoning the rebuild fold.
    """
    payload = completion.get("forwardValidation")
    if not isinstance(payload, Mapping) or not payload:
        return "completion field 'forwardValidation' must be a non-empty object"
    errors = forward_payload_errors(payload)
    if not errors:
        try:
            ForwardEvaluation.from_dict(payload)
        except WorkflowError as error:
            errors = [str(error)]
    if errors:
        return format_contract_errors(errors)
    return None


def _creation_validator(completion: Mapping[str, object]) -> str | None:
    """Deeply validate the creation contract before any transition commits.

    The creation handoff is the one contract a deterministic phase consumes,
    so the payload is validated completely here — inside the engine's
    pre-transition validator — instead of first failing inside the trials
    phase after the state has already moved.  Every missing or wrongly-typed
    field is reported at once so the agent repairs the whole contract in one
    turn.
    """
    payload = completion.get("hypothesis")
    if not isinstance(payload, Mapping) or not payload:
        return "completion field 'hypothesis' must be a non-empty object"
    errors = hypothesis_payload_errors(payload)
    if not errors:
        # Drift guard: the collector must be at least as strict as the
        # composer the deterministic successor phase runs, so anything the
        # collector missed still rejects the contract here — never later,
        # after the transition has committed.
        try:
            _draft_from_contract(payload)
        except (FalsificationComposeError, WorkflowError) as error:
            errors = [str(error)]
    if errors:
        return format_contract_errors(errors)
    return None


def _strategy_receipt_validator(completion: Mapping[str, object]) -> str | None:
    """Check receipt structure; composition verifies its persisted provenance."""
    receipt = completion.get("strategy")
    if not isinstance(receipt, Mapping):
        return "completion field 'strategy' must be a strategy authoring receipt"
    misses = validate_arguments(PROGRAM_REFERENCE_SCHEMA, receipt)
    if misses:
        return "invalid strategy authoring receipt: " + render_violations(misses)
    if receipt["kind"] != "strategy":
        return "strategy completion must reference a strategy program"
    return None


def _strategy_backtest_validator(completion: Mapping[str, object]) -> str | None:
    """Validate the strategy backtest handoff before any transition commits.

    The typed :class:`StrategyBacktest` decoder re-runs as a drift guard.  Job
    provenance (the job must be this phase's retained run of the frozen
    strategy) is checked by the session services, which hold the job records.
    """
    payload = completion.get("backtest")
    if not isinstance(payload, Mapping) or not payload:
        return "completion field 'backtest' must be a non-empty object"
    errors = strategy_backtest_payload_errors(payload)
    if not errors:
        try:
            StrategyBacktest.from_dict(payload)
        except WorkflowError as error:
            errors = [str(error)]
    if errors:
        return format_contract_errors(errors)
    return None


_RCA_FAILURE_CLASSES = frozenset(
    ("harness", "data", "implementation", "strategy", "market_regime")
)


def _rca_validator(completion: Mapping[str, object]) -> str | None:
    """Validate one strategy root-cause diagnosis before any transition commits."""
    payload = completion.get("diagnosis")
    if not isinstance(payload, Mapping) or not payload:
        return "completion field 'diagnosis' must be a non-empty object"
    errors: list[str] = []
    if payload.get("failureClass") not in _RCA_FAILURE_CLASSES:
        errors.append(
            "diagnosis.failureClass must be harness, data, implementation, "
            "strategy, or market_regime"
        )
    try:
        RcaDiagnosis.from_dict(payload)
    except WorkflowError as error:
        errors.append(str(error))
    if errors:
        return format_contract_errors(errors)
    return None


def _phase_completion_schema(
    phase: HypothesisDevelopmentPhase,
    required: tuple[str, ...],
    targets: tuple[str, ...],
) -> Mapping[str, object] | None:
    """Return the completion JSON Schema advertised for one phase's agent.

    Discovery and model creation carry their full deep schemas; every other
    agent phase advertises its required handoff fields and the declared target
    enum, so the provider constrains the contract at decode time instead of
    relying on prompt text alone.
    """
    if phase is HypothesisDevelopmentPhase.DISCOVERY:
        return DISCOVERY_COMPLETION_SCHEMA
    if phase is HypothesisDevelopmentPhase.MODEL_CREATION:
        return HYPOTHESIS_COMPLETION_SCHEMA
    if phase is HypothesisDevelopmentPhase.VERDICT:
        advertised_targets = tuple(
            target for target in targets if target != _FAILED.value
        )
        return {
            "type": "object",
            "required": ["verdict"],
            "properties": {
                "target": {"type": "string", "enum": list(advertised_targets)},
                "verdict": VERDICT_PAYLOAD_SCHEMA,
            },
        }
    if phase is HypothesisDevelopmentPhase.STRATEGY_DESIGN:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["strategy"],
            "properties": {
                "target": {"type": "string", "enum": ["strategy_backtesting"]},
                "strategy": {
                    **PROGRAM_REFERENCE_SCHEMA,
                    "properties": {
                        **cast(
                            dict[str, object], PROGRAM_REFERENCE_SCHEMA["properties"]
                        ),
                        "kind": {"type": "string", "enum": ["strategy"]},
                    },
                },
            },
        }
    if phase is HypothesisDevelopmentPhase.OPTIMIZATION:
        advertised_targets = tuple(
            target for target in targets if target != _FAILED.value
        )
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["optimization"],
            "properties": {
                "target": {"type": "string", "enum": list(advertised_targets)},
                "optimization": CANDIDATE_EVALUATION_PAYLOAD_SCHEMA,
            },
        }
    if phase is HypothesisDevelopmentPhase.NULL_TESTING:
        advertised_targets = tuple(
            target for target in targets if target != _FAILED.value
        )
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["nullTesting"],
            "properties": {
                "target": {"type": "string", "enum": list(advertised_targets)},
                "nullTesting": NULL_TEST_EVALUATION_PAYLOAD_SCHEMA,
            },
        }
    if phase is HypothesisDevelopmentPhase.VARIATION:
        advertised_targets = tuple(
            target for target in targets if target != _FAILED.value
        )
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["variation"],
            "properties": {
                "target": {"type": "string", "enum": list(advertised_targets)},
                "variation": VARIATION_PAYLOAD_SCHEMA,
            },
        }
    if phase is HypothesisDevelopmentPhase.FORWARD_VALIDATION:
        advertised_targets = tuple(
            target for target in targets if target != _FAILED.value
        )
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["forwardValidation"],
            "properties": {
                "target": {"type": "string", "enum": list(advertised_targets)},
                "forwardValidation": FORWARD_PAYLOAD_SCHEMA,
            },
        }
    # The advertised target enum names only real successors: the engine still
    # accepts a terminal failure target, but the advertised shape never
    # invites the agent to end the whole run itself.
    targets = tuple(target for target in targets if target != _FAILED.value)
    if not required and not targets:
        return None
    properties: dict[str, object] = {
        "target": {"type": "string", "enum": targets},
    }
    for name in required:
        properties[name] = {"type": "object"}
    schema: dict[str, object] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


@dataclass(frozen=True, slots=True)
class PhaseDeclaration:
    """One authoritative phase, executor policy, and outgoing edge declaration."""

    spec: PhaseSpec[
        DevelopmentActivities, HypothesisDevelopmentRequest, HypothesisDevelopmentState
    ]
    edges: Mapping[str, PipingFunction[HypothesisDevelopmentState]]
    orchestrator: tuple[str, str] | None
    task: str | None
    activity: str | None
    mcp_tools: tuple[str, ...]
    tools: tuple[str, ...]
    knowledge: tuple[str, ...] | None
    payload_fold: PayloadFold | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "edges", MappingProxyType(dict(self.edges)))


@cache
def phase_declarations() -> tuple[PhaseDeclaration, ...]:
    """Declare every live phase; all secondary lookup maps derive from here."""
    phase = HypothesisDevelopmentPhase
    knowledge_tools = ("search_knowledge", "read_knowledge")
    sketch_tools = (
        "author_strategy_sketch",
        "read_strategy_sketch",
        "update_strategy_sketch",
        "list_strategy_sketches",
    )
    program_tools = (
        tuple(
            f"{operation}_{kind}"
            for kind in ("study", "strategy", "indicator")
            for operation in ("author", "update", "read", "validate", "publish")
        )
        + tuple(f"list_{plural}" for plural in ("studies", "strategies", "indicators"))
        + ("read_sdk_reference",)
    )
    backtest_tools = (
        *knowledge_tools,
        "read_strategy",
        "run_backtest",
        "read_sdk_reference",
    )
    candidate_tools = (*backtest_tools, "run_backtest_batch")
    null_testing_tools = (*backtest_tools, "run_null_batch")
    variation_tools = (*backtest_tools, "run_backtest_batch")
    forward_tools = (*backtest_tools, "run_backtest_batch", "run_null_batch")
    run_evidence_tools = (
        "get_job_status",
        "get_job_result",
        "list_artifacts",
        "read_artifact",
    )
    backtest_execution_guidance = (
        "Use the handed-off strategy.programId with read_strategy and "
        "run_backtest {program_id: ...}. Execution uses the latest committed "
        "content and requires its current accepted validation. Use read_sdk_reference "
        "for exact SDK signatures, not guessed APIs. If validation is stale or "
        "rejected, report the blocker rather than supplying inline source or "
        "bypassing validation. Launch acceptance is not a successful completed run: "
        "never assume launch success means results are available. Retain the returned "
        "job_id and inspect get_job_status {job_id: ...} while queued or "
        "running. After terminal status, call get_job_result {job_id: ...}, "
        "list_artifacts {job_id: ...} and read_artifact {job_id: ..., name: ...} to "
        "inspect actual "
        "metrics and evidence. Record terminal failures honestly; do not fabricate "
        "metrics or artifacts. Include strategy.programId and the actual executed "
        "version/gitCommit from the launch receipt in the completion handoff so the "
        "next phase can identify the latest job. Request phase completion yourself "
        "only after inspecting the terminal outcome. These are agent instructions, "
        "not runtime polling or repair caps."
    )

    def agent(
        name: HypothesisDevelopmentPhase,
        kind: type[ConversationPhase] | type[OneShotPhase],
        edges: Mapping[str, PipingFunction[HypothesisDevelopmentState]],
        family: str,
        label: str,
        task: str,
        activity: str,
        required: str,
        validator: Callable[[Mapping[str, object]], str | None],
        seed: Callable[..., str],
        payload_fold: PayloadFold | None,
        mcp_tools: tuple[str, ...] = (),
        tools: tuple[str, ...] = knowledge_tools,
        knowledge: tuple[str, ...] | None = None,
    ) -> PhaseDeclaration:
        targets = tuple(edges)
        return PhaseDeclaration(
            spec=kind(
                phase=name.value,
                validate=validator,
                required=(required,),
                seed=seed,
                completion_schema=_phase_completion_schema(name, (required,), targets),
            ),
            edges=edges,
            orchestrator=(family, label),
            task=task,
            activity=activity,
            mcp_tools=mcp_tools,
            tools=tools,
            knowledge=knowledge,
            payload_fold=payload_fold,
        )

    declarations = (
        agent(
            phase.DISCOVERY,
            ConversationPhase,
            {
                phase.MODEL_CREATION.value: reset_premise,
                phase.COMPLETED.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            "research",
            "Thesis discovery orchestrator",
            "Develop an evolving strategy sketch from the premise using the knowledge tool bag, authored components and exploratory outputs; investigate and revise its mechanism, representations, conditioning and trading ideas.",
            "Developing and investigating an evolving strategy sketch.",
            "discovery",
            _discovery_validator,
            _discovery_seed,
            _fold_discovery,
            (
                "get_job_status",
                "get_job_result",
                "list_artifacts",
                "read_artifact",
            ),
            tools=(
                *knowledge_tools,
                "set_research_window",
                "start_workflow",
                "read_research_investigations",
                "await_sub_agent",
                *sketch_tools,
                *program_tools,
                "run_study",
                "run_backtest",
                "run_backtest_batch",
                "run_null_batch",
                "read_study_output",
            ),
            knowledge=(
                "discovery",
                "data-quality",
                "trading-costs",
                "model-cards",
                "inference",
                "validation",
            ),
        ),
        agent(
            phase.MODEL_CREATION,
            ConversationPhase,
            {
                phase.FALSIFICATION_TRIALS.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            "research",
            "Thesis model-creation orchestrator",
            "Prepare executable falsification trials tied to the accepted sketch assumptions and pinned components.",
            "Finalizing the structured hypothesis model.",
            "hypothesis",
            _creation_validator,
            _model_creation_seed,
            _fold_draft,
            tools=(*knowledge_tools, "read_strategy_sketch", *program_tools),
            knowledge=("model-cards", "inference", "validation", "trading-costs"),
        ),
        PhaseDeclaration(
            spec=DeterministicPhase(
                phase=phase.FALSIFICATION_TRIALS.value,
                contract=run_falsification_trials,
                validate=_trials_validator,
                required=("trials", "research"),
                on_complete=phase.VERDICT.value,
            ),
            edges={
                phase.VERDICT.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            orchestrator=None,
            task=None,
            activity=None,
            mcp_tools=(),
            tools=(),
            knowledge=None,
            payload_fold=_fold_trials,
        ),
        agent(
            phase.VERDICT,
            ConversationPhase,
            {
                phase.STRATEGY_DESIGN.value: identity_pipe,
                phase.MODEL_CREATION.value: reset_premise,
                phase.DISCOVERY.value: reset_discovery,
                phase.FAILED.value: identity_pipe,
            },
            "research",
            "Thesis research verdict orchestrator",
            "Record the falsification verdict from the trial results.",
            "Recording the falsification verdict.",
            "verdict",
            _verdict_validator,
            _verdict_seed,
            _fold_verdict,
        ),
        agent(
            phase.STRATEGY_DESIGN,
            ConversationPhase,
            {
                phase.STRATEGY_BACKTESTING.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            "coding",
            "Thesis coding strategy-design orchestrator",
            "Implement a sandbox-valid strategy whose exits re-check the hypothesis falsifiers.",
            "Preparing the strategy design from the surviving model.",
            "strategy",
            _strategy_receipt_validator,
            _strategy_design_seed,
            _fold_strategy_design,
            tools=(*knowledge_tools, "read_strategy_sketch", *program_tools),
        ),
        agent(
            phase.STRATEGY_BACKTESTING,
            OneShotPhase,
            {
                phase.OPTIMIZATION.value: identity_pipe,
                phase.STRATEGY_RCA.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            "coding",
            "Thesis strategy-backtesting orchestrator",
            "Run the designed strategy through the harness and record its test result.",
            "Testing the designed strategy through the harness.",
            "backtest",
            _strategy_backtest_validator,
            _contract_seed(
                "Run the designed strategy through the harness and record its test result. "
                + backtest_execution_guidance
                + " Complete with target optimization when the backtest succeeded, "
                "or target strategy_rca when the evidence shows the design is broken."
            ),
            _fold_strategy_backtesting,
            mcp_tools=run_evidence_tools,
            tools=backtest_tools,
        ),
        agent(
            phase.STRATEGY_RCA,
            OneShotPhase,
            {
                phase.STRATEGY_DESIGN.value: reset_backtests,
                phase.FAILED.value: identity_pipe,
            },
            "critique",
            "Thesis strategy root-cause orchestrator",
            "Diagnose the failed strategy backtest and prescribe a concrete revision.",
            "Diagnosing the failed strategy backtest.",
            "diagnosis",
            _rca_validator,
            _contract_seed(
                "Diagnose the failed strategy backtest and prescribe a concrete revision. "
                "Complete with target strategy_design once the revision is recorded.",
                schema=_RCA_SCHEMA,
            ),
            _fold_strategy_rca,
        ),
        agent(
            phase.OPTIMIZATION,
            OneShotPhase,
            {
                phase.NULL_TESTING.value: identity_pipe,
                phase.STRATEGY_RCA.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            "coding",
            "Thesis coding optimization orchestrator",
            "Evaluate a bounded set of parameter candidates for the designed strategy and select a shortlist.",
            "Preparing the bounded candidate evaluation batch.",
            "optimization",
            _optimization_validator,
            _optimization_seed,
            _fold_optimization,
            mcp_tools=run_evidence_tools,
            tools=candidate_tools,
        ),
        agent(
            phase.NULL_TESTING,
            OneShotPhase,
            {
                phase.VARIATION.value: identity_pipe,
                phase.STRATEGY_RCA.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            "coding",
            "Thesis null-testing orchestrator",
            "Freeze a supported permutation method and sufficient seeds, submit bounded chunks, and record the observed statistic, conservative exceedance count, and conditional p-value honestly.",
            "Running the declared permutation null experiment.",
            "nullTesting",
            _null_testing_validator,
            _null_testing_seed,
            _fold_null_testing,
            mcp_tools=run_evidence_tools,
            tools=null_testing_tools,
        ),
        agent(
            phase.VARIATION,
            OneShotPhase,
            {
                phase.FORWARD_VALIDATION.value: identity_pipe,
                phase.STRATEGY_RCA.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            "coding",
            "Thesis variation orchestrator",
            "Measure a deterministic one-at-a-time parameter neighborhood around the shortlisted candidate and assess whether the edge sits on a stable plateau.",
            "Running the deterministic one-at-a-time parameter neighborhood.",
            "variation",
            _variation_validator,
            _variation_seed,
            _fold_variation,
            mcp_tools=run_evidence_tools,
            tools=variation_tools,
        ),
        agent(
            phase.FORWARD_VALIDATION,
            OneShotPhase,
            {
                phase.COMPLETED.value: identity_pipe,
                phase.STRATEGY_RCA.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            "coding",
            "Thesis forward-validation orchestrator",
            "Re-run the frozen center, null design, and variation points on the protected forward window and record the honest development-versus-forward comparison.",
            "Running the protected forward out-of-sample validation.",
            "forwardValidation",
            _forward_validator,
            _forward_validation_seed,
            _fold_forward_validation,
            mcp_tools=run_evidence_tools,
            tools=forward_tools,
        ),
        PhaseDeclaration(
            TerminalPhase(phase=phase.COMPLETED.value),
            {},
            None,
            None,
            None,
            (),
            (),
            None,
            None,
        ),
        PhaseDeclaration(
            TerminalPhase(phase=phase.FAILED.value),
            {},
            None,
            None,
            None,
            (),
            (),
            None,
            None,
        ),
    )
    names = [item.spec.phase for item in declarations]
    if len(set(names)) != len(names) or set(names) != {item.value for item in phase}:
        raise WorkflowError(
            "phase declarations must cover every live phase exactly once"
        )
    return declarations


def piping() -> Mapping[tuple[str, str], PipingFunction[HypothesisDevelopmentState]]:
    """Return the real pipe for every declared transition, by phase name."""
    return {
        (item.spec.phase, target): pipe
        for item in phase_declarations()
        for target, pipe in item.edges.items()
    }


def build_hypothesis_development_spec() -> WorkflowSpec[
    DevelopmentActivities,
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
]:
    """Assemble and validate the hypothesis-development workflow spec."""
    return build_hypothesis_development_workflow().spec


def build_hypothesis_development_workflow(
    *,
    chat: ChatService | None = None,
    runner: BacktestRunner | None = None,
    research_runner: ResearchModelRunner | None = None,
    programs: ProgramSnapshots | None = None,
    hypothesis_store: HypothesisStore | None = None,
    clock: Callable[[], datetime] = _utc_now,
    id_factory: Callable[[str], str] = _new_id,
    experiment_id: str | None = None,
    subagent_mcp_tools: Mapping[str, tuple[str, ...]] | None = None,
    conversations: Conversations | None = None,
    execution_store: ExecutionStore | None = None,
    detach: Detach | None = None,
    work_store: WorkStore | None = None,
    knowledge_scope: Literal["shared", "workspace", "both"] = "both",
) -> Workflow[
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
    HypothesisDevelopmentOutcome,
]:
    """Build the hypothesis-development workflow as the general object.

    The same construction path serves the registered specification (no ports)
    and the production runtime (all ports, services, and doctrine bound).
    """
    from .doctrine import hypothesis_development_doctrine
    from .phases import workflow_definition

    declarations = phase_declarations()
    definition = workflow_definition()
    resolved_research_runner = research_runner
    if resolved_research_runner is None and runner is not None:
        resolved_research_runner = BacktestResearchModelRunner(runner)
    if resolved_research_runner is not None and not callable(
        getattr(resolved_research_runner, "run", None)
    ):
        raise WorkflowError("research_runner must implement ResearchModelRunner")
    tools: dict[str, tuple[str, ...]] = {
        family: tuple(family_tools)
        for family, family_tools in dict(subagent_mcp_tools or {}).items()
    }
    services = HypothesisDevelopmentServices(
        store=(
            InMemoryHypothesisStore() if hypothesis_store is None else hypothesis_store
        ),
        research_runner=resolved_research_runner,
        programs=programs,
        experiment_id=experiment_id,
        mcp_tools=tools,
        phase_mcp_tools={item.spec.phase: item.mcp_tools for item in declarations},
        phase_tools={item.spec.phase: item.tools for item in declarations},
    )
    builder = WorkflowBuilder[
        DevelopmentActivities,
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
    ]()
    builder.workflow(
        definition.workflow_id,
        version=definition.version,
        title=definition.title,
    )
    for item in declarations:
        builder.phase(item.spec.host_spec())
    builder.routes(
        {
            item.spec.phase: {target: (f"{target}.input",) for target in item.edges}
            for item in declarations
        }
    )
    from .endpoint import HypothesisExtension

    extension = HypothesisExtension(
        services,
        chat,
        execution_store,
        clock,
        id_factory,
        knowledge_scope=knowledge_scope,
        phase_knowledge={
            item.spec.phase: item.knowledge
            for item in declarations
            if item.knowledge is not None
        },
    )
    builder.doctrine(hypothesis_development_doctrine(services, extension))
    if chat is not None:
        builder.ports(
            WorkflowPorts(
                chat=chat,
                clock=clock,
                id_factory=id_factory,
                conversations=conversations,
                execution_store=execution_store,
                detach=detach,
                work_store=work_store,
            )
        )
    return cast(Any, builder.build())
