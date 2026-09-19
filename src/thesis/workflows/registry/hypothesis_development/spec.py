"""Assemble the hypothesis-development workflow specification and object.

The spec is the registered definition of the concrete workflow: its ordered
phases, the allowed transition table, one real executor per phase, and the
typed piping between phases.
Every phase is bound to the executor module that owns its behavior; the state
machine wiring lives here instead of inside a workflow class.  The builder then
assembles the one general :class:`~thesis.workflows.core.workflow.Workflow`
instance from that spec, the doctrine, the services bundle, and the ports.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from thesis.agent.router import ModelRouter, Role
from thesis.agent.runtime.delivery import TurnScheduler
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.subagents import SubagentContextFactory
from thesis.chat.service import ChatService
from thesis.experiments.ledger import AttemptLedger
from thesis.persistence.journal import CommandJournal
from thesis.research.hypothesis import HypothesisStore, InMemoryHypothesisStore
from thesis.strategies.validation import StrategySandboxValidator
from thesis.workflows.backtests import BacktestRunner
from thesis.workflows.core.builder import WorkflowBuilder
from thesis.workflows.core.phase import PhaseExecutor
from thesis.workflows.core.spec import HandoffContext, PipingFunction, WorkflowSpec
from thesis.workflows.core.workflow import Workflow, WorkflowPorts
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ResearchModelRunner,
)

from .composers.hypothesis import JsonDevelopmentHypothesisComposer
from .composers.plan import JsonFalsificationPlanner
from .composers.rca import JsonRcaComposer
from .composers.strategy import JsonStrategyDesigner
from .contracts.creation import HypothesisDevelopmentRequest
from .doctrine import hypothesis_development_doctrine
from .errors import WorkflowError
from .phases import (
    _COMPLETED,
    _FAILED,
    _PHASE_TASKS,
    _TRANSITIONS,
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    HypothesisDevelopmentPhase,
)
from .phases.backtesting import run_strategy_backtest
from .phases.creation import run_creation_turn
from .phases.design import advance_design
from .phases.lifecycle import advance_inert, run_requested_turn
from .phases.optimization import optimize
from .phases.strategy_rca import diagnose_strategy
from .phases.trials import advance_trials
from .phases.verdict import advance_verdict
from .piping import identity_pipe, reset_backtests, reset_premise
from .primitives import (
    _FALSIFICATION_PLAN_SCHEMA,
    _HYPOTHESIS_SCHEMA,
    _RCA_SCHEMA,
    _STRATEGY_DESIGN_SCHEMA,
    DEFAULT_MAX_COMPOSE_REPAIRS,
    DEFAULT_MAX_HYPOTHESIS_CYCLES,
    DEFAULT_MAX_RCA_CYCLES,
    _new_id,
    _utc_now,
)
from .prompts.creation import HypothesisComposer
from .prompts.falsification import FalsificationPlanner
from .prompts.rca import RcaComposer
from .prompts.strategy import StrategyDesigner
from .services import (
    DevelopmentEngine,
    HypothesisDevelopmentServices,
)
from .state import HypothesisDevelopmentOutcome, HypothesisDevelopmentState

__all__ = [
    "build_hypothesis_development_spec",
    "build_hypothesis_development_workflow",
    "phase_executors",
    "piping",
]

_TERMINAL_PHASES: frozenset[HypothesisDevelopmentPhase] = frozenset(
    {_COMPLETED, _FAILED}
)

_EXECUTORS: Mapping[
    HypothesisDevelopmentPhase,
    PhaseExecutor[
        DevelopmentEngine,
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
    ],
] = {
    HypothesisDevelopmentPhase.REQUESTED: run_requested_turn,
    HypothesisDevelopmentPhase.HYPOTHESIS_CREATION: run_creation_turn,
    HypothesisDevelopmentPhase.FALSIFICATION_TRIALS: advance_trials,
    HypothesisDevelopmentPhase.RCA: advance_inert,
    HypothesisDevelopmentPhase.VERDICT: advance_verdict,
    HypothesisDevelopmentPhase.STRATEGY_DESIGN: advance_design,
    HypothesisDevelopmentPhase.STRATEGY_BACKTESTING: run_strategy_backtest,
    HypothesisDevelopmentPhase.STRATEGY_RCA: diagnose_strategy,
    HypothesisDevelopmentPhase.OPTIMIZATION: optimize,
    HypothesisDevelopmentPhase.COMPLETED: advance_inert,
    HypothesisDevelopmentPhase.FAILED: advance_inert,
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
    """Seed the creation agent with its instructions and the operator's words."""
    del state, context
    return _creation_prompt(workflow, request)


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
    HypothesisDevelopmentPhase.HYPOTHESIS_CREATION: _mapping_field("hypothesis"),
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
    }
    for phase, task in _PHASE_TASKS.items():
        if phase in seeds or phase not in _REQUIRED:
            continue
        seeds[phase] = _contract_seed(
            task,
            schema=_SCHEMAS.get(phase),
        )
    return seeds


def phase_executors() -> Mapping[
    HypothesisDevelopmentPhase,
    PhaseExecutor[
        DevelopmentEngine,
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
    ],
]:
    """Return the real executor for every declared phase."""
    return dict(_EXECUTORS)


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
    composer: HypothesisComposer | None = None,
    planner: FalsificationPlanner | None = None,
    rca_composer: RcaComposer | None = None,
    designer: StrategyDesigner | None = None,
    ledger: AttemptLedger | None = None,
    subagent_factory: SubagentContextFactory | None = None,
    journal: CommandJournal | None = None,
    clock: Callable[[], datetime] = _utc_now,
    id_factory: Callable[[str], str] = _new_id,
    research_role: Role = "research",
    critique_role: Role = "critique",
    coding_role: Role = "coding",
    experiment_id: str | None = None,
    max_rca_cycles: int = DEFAULT_MAX_RCA_CYCLES,
    max_compose_repairs: int = DEFAULT_MAX_COMPOSE_REPAIRS,
    knowledge_cutoff: datetime | None = None,
    subagent_mcp_tools: Mapping[str, tuple[str, ...]] | None = None,
    subagent_lifecycle: SubAgentLifecycle | None = None,
    turn_scheduler: TurnScheduler | None = None,
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
    if not isinstance(max_rca_cycles, int) or max_rca_cycles < 0:
        raise WorkflowError("max_rca_cycles must be a non-negative integer")
    if not isinstance(max_compose_repairs, int) or max_compose_repairs < 0:
        raise WorkflowError("max_compose_repairs must be a non-negative integer")
    tools: dict[str, tuple[str, ...]] = {
        family: tuple(family_tools)
        for family, family_tools in dict(subagent_mcp_tools or {}).items()
    }
    services = HypothesisDevelopmentServices(
        store=(
            InMemoryHypothesisStore() if hypothesis_store is None else hypothesis_store
        ),
        composer=(
            JsonDevelopmentHypothesisComposer() if composer is None else composer
        ),
        planner=JsonFalsificationPlanner() if planner is None else planner,
        rca_composer=JsonRcaComposer() if rca_composer is None else rca_composer,
        designer=JsonStrategyDesigner() if designer is None else designer,
        router=router,
        runner=runner,
        research_runner=resolved_research_runner,
        validator=validator,
        ledger=ledger,
        journal=journal,
        research_role=research_role,
        critique_role=critique_role,
        coding_role=coding_role,
        experiment_id=experiment_id,
        max_hypothesis_cycles=DEFAULT_MAX_HYPOTHESIS_CYCLES,
        max_rca_cycles=max_rca_cycles,
        max_compose_repairs=max_compose_repairs,
        mcp_tools=tools,
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
        builder.phase(
            phase.value,
            _EXECUTORS[phase],
            terminal=phase in _TERMINAL_PHASES,
            on_complete=targets[0] if len(targets) == 1 else None,
            seed=seeds.get(phase),
            validate=_VALIDATORS.get(phase),
            required=_REQUIRED.get(phase, ()),
        )
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
                subagent_lifecycle=subagent_lifecycle,
                turn_scheduler=turn_scheduler,
            )
        )
    return cast(Any, builder.build())
