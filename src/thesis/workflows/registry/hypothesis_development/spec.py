"""Assemble the hypothesis-development workflow specification and object.

The spec is the registered definition of the concrete workflow: its ordered
phases, the allowed transition table, and the typed piping between phases.
Every phase is declared as a sealed-union phase object — conversation,
one-shot, or deterministic — bound to the module that owns its behavior; the
state machine wiring lives here instead of inside a workflow class.  The
builder then assembles the one general
:class:`~thesis.workflows.core.workflow.Workflow` instance from that spec, the
doctrine, the services bundle, and the ports.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from thesis.agent.router import ModelRouter, Role
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.subagents import SubagentContextFactory
from thesis.chat.service import ChatService
from thesis.experiments.ledger import AttemptLedger
from thesis.persistence.journal import CommandJournal
from thesis.research.hypothesis import HypothesisStore, InMemoryHypothesisStore
from thesis.strategies.validation import StrategySandboxValidator
from thesis.workflows.backtests import BacktestRunner
from thesis.workflows.core.builder import WorkflowBuilder
from thesis.workflows.core.execution_store import ExecutionStore
from thesis.workflows.core.phase import (
    ConversationPhase,
    DeterministicExecutor,
    DeterministicPhase,
    OneShotPhase,
    PhaseSpec,
)
from thesis.workflows.core.spec import HandoffContext, PipingFunction, WorkflowSpec
from thesis.workflows.core.workflow import Detach, Workflow, WorkflowPorts
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ResearchModelRunner,
)

from .contracts.creation import HypothesisDevelopmentRequest
from .contracts.validation import (
    HYPOTHESIS_COMPLETION_SCHEMA,
    format_contract_errors,
    hypothesis_payload_errors,
)
from .doctrine import hypothesis_development_doctrine
from .errors import FalsificationComposeError, WorkflowError
from .phases import (
    _COMPLETED,
    _FAILED,
    _PHASE_TASKS,
    _TRANSITIONS,
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    HypothesisDevelopmentPhase,
)
from .phases.trials import _draft_from_contract, run_falsification_trials
from .piping import identity_pipe, reset_backtests, reset_premise
from .primitives import (
    _FALSIFICATION_PLAN_SCHEMA,
    _HYPOTHESIS_SCHEMA,
    _RCA_SCHEMA,
    _STRATEGY_DESIGN_SCHEMA,
    _new_id,
    _utc_now,
)
from .services import (
    DevelopmentEngine,
    HypothesisDevelopmentServices,
)
from .state import HypothesisDevelopmentOutcome, HypothesisDevelopmentState

__all__ = [
    "build_hypothesis_development_spec",
    "build_hypothesis_development_workflow",
    "piping",
]

_TERMINAL_PHASES: frozenset[HypothesisDevelopmentPhase] = frozenset(
    {_COMPLETED, _FAILED}
)

#: The phases whose agents accept operator input from birth.
_CONVERSATIONAL_PHASES: frozenset[HypothesisDevelopmentPhase] = frozenset(
    {HypothesisDevelopmentPhase.HYPOTHESIS_CREATION, HypothesisDevelopmentPhase.VERDICT}
)

#: MCP tools each agent-backed phase may call, by phase. The creation agent
#: authors the Rust study program, so it gets the compilation gate to check its
#: own draft; every other phase authors nothing and declares no MCP tools.
#: This is the one place the phase tool surface is configured; the agent-facing
#: catalog is exactly this list plus the structural phase control tools.
_PHASE_MCP_TOOLS: Mapping[str, tuple[str, ...]] = {
    HypothesisDevelopmentPhase.HYPOTHESIS_CREATION.value: ("validate_study",),
}

#: The deterministic contract function for each deterministic phase.
_CONTRACTS: Mapping[
    HypothesisDevelopmentPhase,
    DeterministicExecutor[
        DevelopmentEngine,
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
    ],
] = {
    HypothesisDevelopmentPhase.FALSIFICATION_TRIALS: run_falsification_trials,
}

#: Typed state pipes that replace the state movement the phases used to inline.
_PIPES: Mapping[
    tuple[HypothesisDevelopmentPhase, HypothesisDevelopmentPhase],
    PipingFunction[HypothesisDevelopmentState],
] = {
    (
        HypothesisDevelopmentPhase.VERDICT,
        HypothesisDevelopmentPhase.HYPOTHESIS_CREATION,
    ): reset_premise,
    (
        HypothesisDevelopmentPhase.STRATEGY_RCA,
        HypothesisDevelopmentPhase.STRATEGY_DESIGN,
    ): reset_backtests,
}


def _start_handoff(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    context: HandoffContext,
) -> str | None:
    """Create the first phase's prompt from the operator's launching message.

    The workflow starts in hypothesis creation, so the start adapter builds the
    creation prompt around the caller's message, verbatim.  A non-start handoff
    returns ``None`` so each later phase keeps its declared seed.
    """
    if context.source is not None:
        return None
    return _creation_prompt(workflow, request)


def _creation_prompt(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
) -> str:
    """Build the creation prompt: phase instructions plus the operator's words."""
    from .phases.creation import trigger_text
    from .prompts.creation import _creation_turn_prompt

    return _creation_turn_prompt([("user", trigger_text(workflow, request))])


def _creation_seed(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the creation agent with its instructions and the operator's words.

    A loop-back re-entry is a revision, not a fresh start: the seed appends the
    recorded verdict and the falsification-trials report so the agent revises
    the hypothesis instead of repeating the refuted one.
    """
    del state
    prompt = _creation_prompt(workflow, request)
    if context is None or context.source is None:
        return prompt
    from .prompts.creation import _creation_revision_section

    report = _latest_phase_artifact(workflow, request, "falsification_trials")
    return (
        prompt
        + "\n\n"
        + _creation_revision_section(
            dict(context.completion) if context.completion else None, report
        )
    )


def _latest_phase_artifact(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    phase: str,
) -> Mapping[str, object] | None:
    """Return the latest recorded artifact for one phase, if any.

    The recorded report outlives the state reset a loop-back applies, so the
    revision seed reads it from the durable execution events instead of the
    cleared typed state.
    """
    from thesis.execution.events import ExecutionEventType

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


def _verdict_seed(
    workflow: Workflow[Any, Any, Any],
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
    del workflow, request, state
    from .prompts.verdict import _verdict_instructions

    parts = [_verdict_instructions()]
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
        workflow: Workflow[Any, Any, Any],
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


def _mapping_field(name: str) -> Callable[[Mapping[str, object]], str | None]:
    """Reject a completion whose ``name`` field is not a non-empty object."""

    def validate(completion: Mapping[str, object]) -> str | None:
        value = completion.get(name)
        if not isinstance(value, Mapping) or not value:
            return f"completion field {name!r} must be a non-empty object"
        return None

    return validate


def _list_field(name: str) -> Callable[[Mapping[str, object]], str | None]:
    """Reject a completion whose ``name`` field is not a non-empty list."""

    def validate(completion: Mapping[str, object]) -> str | None:
        value = completion.get(name)
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes))
            or not value
        ):
            return f"completion field {name!r} must be a non-empty list"
        return None

    return validate


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


def _phase_completion_schema(
    phase: HypothesisDevelopmentPhase,
) -> Mapping[str, object] | None:
    """Return the completion JSON Schema advertised for one phase's agent.

    The creation phase carries its full deep schema; every other agent phase
    advertises its required handoff fields and the declared target enum, so
    the provider constrains the contract at decode time instead of relying
    on prompt text alone.
    """
    if phase is HypothesisDevelopmentPhase.HYPOTHESIS_CREATION:
        return HYPOTHESIS_COMPLETION_SCHEMA
    required = _REQUIRED.get(phase, ())
    # The advertised target enum names only real successors: the engine still
    # accepts a terminal failure target, but the advertised shape never
    # invites the agent to end the whole run itself.
    targets = sorted(
        target.value
        for target in _TRANSITIONS.get(phase, frozenset())
        if target is not _FAILED
    )
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


#: The required handoff obligation for each phase.
_REQUIRED: Mapping[HypothesisDevelopmentPhase, tuple[str, ...]] = {
    HypothesisDevelopmentPhase.HYPOTHESIS_CREATION: ("hypothesis",),
    HypothesisDevelopmentPhase.FALSIFICATION_TRIALS: ("trials",),
    HypothesisDevelopmentPhase.VERDICT: ("verdict",),
    HypothesisDevelopmentPhase.STRATEGY_DESIGN: ("strategy",),
    HypothesisDevelopmentPhase.STRATEGY_BACKTESTING: ("backtest",),
    HypothesisDevelopmentPhase.STRATEGY_RCA: ("diagnosis",),
    HypothesisDevelopmentPhase.OPTIMIZATION: ("optimization",),
}

#: The structural validator for each phase's completion contract.
_VALIDATORS: Mapping[
    HypothesisDevelopmentPhase, Callable[[Mapping[str, object]], str | None]
] = {
    HypothesisDevelopmentPhase.HYPOTHESIS_CREATION: _creation_validator,
    HypothesisDevelopmentPhase.FALSIFICATION_TRIALS: _list_field("trials"),
    HypothesisDevelopmentPhase.VERDICT: _mapping_field("verdict"),
    HypothesisDevelopmentPhase.STRATEGY_DESIGN: _mapping_field("strategy"),
    HypothesisDevelopmentPhase.STRATEGY_BACKTESTING: _mapping_field("backtest"),
    HypothesisDevelopmentPhase.STRATEGY_RCA: _mapping_field("diagnosis"),
    HypothesisDevelopmentPhase.OPTIMIZATION: _mapping_field("optimization"),
}

#: The declared shape echoed to the agent for phases with a known payload.
_SCHEMAS: Mapping[HypothesisDevelopmentPhase, str] = {
    HypothesisDevelopmentPhase.HYPOTHESIS_CREATION: _HYPOTHESIS_SCHEMA,
    HypothesisDevelopmentPhase.FALSIFICATION_TRIALS: _FALSIFICATION_PLAN_SCHEMA,
    HypothesisDevelopmentPhase.STRATEGY_DESIGN: _STRATEGY_DESIGN_SCHEMA,
    HypothesisDevelopmentPhase.STRATEGY_RCA: _RCA_SCHEMA,
}


def _seeds() -> Mapping[HypothesisDevelopmentPhase, Callable[..., str]]:
    """Build one prompt seed per phase from its task, obligation, and shape."""
    seeds: dict[HypothesisDevelopmentPhase, Callable[..., str]] = {
        HypothesisDevelopmentPhase.HYPOTHESIS_CREATION: _creation_seed,
        HypothesisDevelopmentPhase.VERDICT: _verdict_seed,
    }
    for phase, task in _PHASE_TASKS.items():
        if phase in seeds or phase not in _REQUIRED:
            continue
        seeds[phase] = _contract_seed(
            task,
            schema=_SCHEMAS.get(phase),
        )
    return seeds


def piping() -> Mapping[tuple[str, str], PipingFunction[HypothesisDevelopmentState]]:
    """Return the real pipe for every declared transition, by phase name."""
    pipes: dict[tuple[str, str], PipingFunction[HypothesisDevelopmentState]] = {}
    for source, targets in _TRANSITIONS.items():
        for target in targets:
            pipe = _PIPES.get((source, target), identity_pipe)
            pipes[(source.value, target.value)] = pipe
    return pipes


def build_hypothesis_development_spec() -> WorkflowSpec[
    DevelopmentEngine,
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
]:
    """Assemble and validate the hypothesis-development workflow spec."""
    return build_hypothesis_development_workflow().spec


def build_hypothesis_development_workflow(
    *,
    chat: ChatService | None = None,
    router: ModelRouter | None = None,
    runner: BacktestRunner | None = None,
    research_runner: ResearchModelRunner | None = None,
    validator: StrategySandboxValidator | None = None,
    hypothesis_store: HypothesisStore | None = None,
    ledger: AttemptLedger | None = None,
    subagent_factory: SubagentContextFactory | None = None,
    journal: CommandJournal | None = None,
    clock: Callable[[], datetime] = _utc_now,
    id_factory: Callable[[str], str] = _new_id,
    research_role: Role = "research",
    experiment_id: str | None = None,
    knowledge_cutoff: datetime | None = None,
    subagent_mcp_tools: Mapping[str, tuple[str, ...]] | None = None,
    conversations: Conversations | None = None,
    execution_store: ExecutionStore | None = None,
    detach: Detach | None = None,
) -> Workflow[
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
    HypothesisDevelopmentOutcome,
]:
    """Build the hypothesis-development workflow as the general object.

    The same construction path serves the registered specification (no ports)
    and the production runtime (all ports, services, and doctrine bound).
    """
    del subagent_factory, knowledge_cutoff
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
        router=router,
        runner=runner,
        research_runner=resolved_research_runner,
        validator=validator,
        ledger=ledger,
        journal=journal,
        research_role=research_role,
        experiment_id=experiment_id,
        mcp_tools=tools,
        phase_mcp_tools=dict(_PHASE_MCP_TOOLS),
    )
    builder = WorkflowBuilder[
        DevelopmentEngine,
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
    ]()
    builder.workflow(
        HYPOTHESIS_DEVELOPMENT_WORKFLOW.workflow_id,
        version=HYPOTHESIS_DEVELOPMENT_WORKFLOW.version,
        title=HYPOTHESIS_DEVELOPMENT_WORKFLOW.title,
    )
    seeds = _seeds()
    for phase in _TRANSITIONS:
        targets = sorted(target.value for target in _TRANSITIONS[phase])
        common = {
            "phase": phase.value,
            "terminal": phase in _TERMINAL_PHASES,
            "validate": _VALIDATORS.get(phase),
            "on_complete": targets[0] if len(targets) == 1 else None,
            "required": _REQUIRED.get(phase, ()),
            "completion_schema": _phase_completion_schema(phase),
        }
        declared: PhaseSpec[
            DevelopmentEngine,
            HypothesisDevelopmentRequest,
            HypothesisDevelopmentState,
        ]
        if phase in _CONTRACTS:
            declared = DeterministicPhase(contract=_CONTRACTS[phase], **common)
        elif phase in _CONVERSATIONAL_PHASES:
            declared = ConversationPhase(seed=seeds.get(phase), **common)
        else:
            declared = OneShotPhase(seed=seeds.get(phase), **common)
        builder.phase(declared)
    builder.transitions(
        {
            phase.value: frozenset(target.value for target in targets)
            for phase, targets in _TRANSITIONS.items()
        }
    )
    for (source, target), pipe in piping().items():
        builder.piping(source, target, pipe)
    builder.on_start(_start_handoff)
    builder.doctrine(hypothesis_development_doctrine(services))
    builder.services(services)
    if chat is not None:
        builder.ports(
            WorkflowPorts(
                chat=chat,
                clock=clock,
                id_factory=id_factory,
                conversations=conversations,
                execution_store=execution_store,
                detach=detach,
            )
        )
    return cast(Any, builder.build())
