"""Compose the Thesis workflow runtime from injected ports.

This module is the composition root for the Thesis workflow verticals.  It
builds the policy-driven :class:`~thesis.agent.router.ModelRouter` from role
policies, model profiles, and a :class:`~thesis.agent.skills.SkillRegistry`,
and builds a :class:`~thesis.workflows.backtests.HarnessBacktestRunner` over
the published :class:`~thesis.integrations.harness.ThesisHarnessClient`
boundary. The resulting :class:`WorkflowRuntime` holder constructs and executes
the falsification-first hypothesis-development workflow against those composed
ports.

Composition stays entirely behind injected boundaries: callers provide the
provider registry, the policy/profile/skill registries, and either a harness
adapter or a replacement :class:`~thesis.workflows.backtests.BacktestRunner`.
Tests inject a fake model provider and a fake backtest runner; production
injects the configured provider and the harness transport.  No HTTP or CLI
code is imported here; the runtime depends only on inward Thesis packages.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from thesis.agent.harness_tools import (
    HarnessToolProvider,
    register_harness_mcp_skill,
)
from thesis.agent.hypothesis_skills import register_hypothesis_development_skills
from thesis.agent.router import (
    ModelProfile,
    ModelProvider,
    ModelRouter,
    PolicyBundle,
    Role,
    RolePolicy,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.skills import SkillRegistry
from thesis.agent.subagents import SubagentContextFactory
from thesis.chat.service import ChatService
from thesis.experiments.ledger import AttemptLedger
from thesis.integrations.harness import HarnessClientPort, ThesisHarnessClient
from thesis.persistence.journal import CommandJournal
from thesis.research.hypothesis import HypothesisStore
from thesis.research.indicators import IndicatorStore
from thesis.research.models import ResearchModelStore
from thesis.strategies.validation import StrategySandboxValidator
from thesis.workflows.backtests import (
    BacktestMode,
    BacktestRunner,
    HarnessBacktestRunner,
)
from thesis.workflows.hypothesis_development import (
    DEFAULT_MAX_RCA_CYCLES,
    FalsificationPlanner,
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentWorkflow,
    RcaComposer,
    StrategyDesigner,
)
from thesis.workflows.hypothesis_development import (
    HypothesisComposer as DevelopmentHypothesisComposer,
)
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ResearchModelRunner,
)


class WorkflowRuntimeError(RuntimeError):
    """Raised when the workflow runtime cannot be composed or executed."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def build_model_router(
    *,
    role_policies: Mapping[Role, RolePolicy],
    model_profiles: Mapping[str, ModelProfile],
    providers: Mapping[str, ModelProvider],
    skills: SkillRegistry,
    bundle: PolicyBundle | None = None,
    bundle_hashes: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] = _utc_now,
    invocation_id_factory: Callable[[], str] | None = None,
) -> ModelRouter:
    """Build a policy-driven model router from injected registries.

    The caller owns every registry: role policies select a model profile and
    skill references, model profiles name a provider and deployment, providers
    implement the invocation boundary, and the skill registry resolves the
    selected skill versions.  The harness MCP skill is always registered into
    ``skills`` so a role policy that selects ``harness-mcp`` resolves its tool
    surface.  When ``invocation_id_factory`` is omitted the router generates
    its own portable invocation identifiers.
    """

    def _default_invocation_id() -> str:
        return _new_id("invocation")

    register_harness_mcp_skill(skills)
    register_hypothesis_development_skills(skills)

    return ModelRouter(
        role_policies=role_policies,
        model_profiles=model_profiles,
        providers=providers,
        skills=skills,
        bundle=bundle,
        bundle_hashes=bundle_hashes,
        clock=clock,
        id_factory=(
            invocation_id_factory
            if invocation_id_factory is not None
            else _default_invocation_id
        ),
    )


def _harness_runner(
    harness: ThesisHarnessClient | HarnessClientPort | None,
    *,
    mode: BacktestMode,
    metadata: Mapping[str, object] | None,
) -> HarnessBacktestRunner:
    if harness is None:
        raise WorkflowRuntimeError(
            "provide a harness client or an injected backtest runner"
        )
    client = (
        harness
        if isinstance(harness, ThesisHarnessClient)
        else ThesisHarnessClient(harness)
    )
    return HarnessBacktestRunner(client, mode=mode, metadata=metadata)


def build_workflow_runtime(
    *,
    role_policies: Mapping[Role, RolePolicy],
    model_profiles: Mapping[str, ModelProfile],
    providers: Mapping[str, ModelProvider],
    skills: SkillRegistry,
    harness: ThesisHarnessClient | HarnessClientPort | None = None,
    runner: BacktestRunner | None = None,
    harness_tools: HarnessToolProvider | None = None,
    strategy_validator: StrategySandboxValidator | None = None,
    hypothesis_store: HypothesisStore | None = None,
    research_model_store: ResearchModelStore | None = None,
    indicator_store: IndicatorStore | None = None,
    research_runner: ResearchModelRunner | None = None,
    bundle: PolicyBundle | None = None,
    bundle_hashes: Mapping[str, str] | None = None,
    backtest_mode: BacktestMode = "local",
    metadata: Mapping[str, object] | None = None,
    subagent_lifecycle: SubAgentLifecycle | None = None,
    clock: Callable[[], datetime] = _utc_now,
    id_factory: Callable[[str], str] = _new_id,
    invocation_id_factory: Callable[[], str] | None = None,
) -> WorkflowRuntime:
    """Compose the model router and backtest runner into one runtime holder.

    A :class:`HarnessBacktestRunner` is built over the injected harness client
    unless an explicit ``runner`` is supplied, so tests can replace the harness
    with a fake while exercising the same runtime surface.  A
    :class:`~thesis.agent.harness_tools.HarnessToolProvider` supplied here is
    held for composition consumers; tool dispatch happens through the unified
    session registry.
    """

    def _invocation_id() -> str:
        return id_factory("invocation")

    router = build_model_router(
        role_policies=role_policies,
        model_profiles=model_profiles,
        providers=providers,
        skills=skills,
        bundle=bundle,
        bundle_hashes=bundle_hashes,
        clock=clock,
        invocation_id_factory=(
            invocation_id_factory
            if invocation_id_factory is not None
            else _invocation_id
        ),
    )
    resolved_runner: BacktestRunner = (
        runner
        if runner is not None
        else _harness_runner(harness, mode=backtest_mode, metadata=metadata)
    )
    resolved_research_runner = (
        research_runner
        if research_runner is not None
        else BacktestResearchModelRunner(
            resolved_runner,
            models=research_model_store,
            indicators=indicator_store,
        )
    )
    return WorkflowRuntime(
        router=router,
        runner=resolved_runner,
        research_runner=resolved_research_runner,
        harness_tools=harness_tools,
        validator=strategy_validator,
        hypothesis_store=hypothesis_store,
        subagent_lifecycle=subagent_lifecycle,
        clock=clock,
        id_factory=id_factory,
    )


class WorkflowRuntime:
    """Holder that builds and executes Thesis workflows over composed ports.

    The runtime owns the composed :class:`ModelRouter` and
    :class:`BacktestRunner`.  Each builder pairs them with an injected
    :class:`ChatService` (plus the workflow-specific validator, composer,
    journal, and importer ports) and returns a runnable workflow.  The matching
    ``execute_*`` helpers run an already-launched request through a freshly
    composed workflow.
    """

    def __init__(
        self,
        *,
        router: ModelRouter,
        runner: BacktestRunner,
        research_runner: ResearchModelRunner | None = None,
        harness_tools: HarnessToolProvider | None = None,
        validator: StrategySandboxValidator | None = None,
        hypothesis_store: HypothesisStore | None = None,
        subagent_lifecycle: SubAgentLifecycle | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
    ) -> None:
        if not isinstance(router, ModelRouter):
            raise WorkflowRuntimeError("router must be a ModelRouter")
        if not callable(getattr(runner, "run", None)):
            raise WorkflowRuntimeError("runner must implement the BacktestRunner port")
        resolved_research_runner = (
            BacktestResearchModelRunner(runner)
            if research_runner is None
            else research_runner
        )
        if not callable(getattr(resolved_research_runner, "run", None)):
            raise WorkflowRuntimeError(
                "research_runner must implement the ResearchModelRunner port"
            )
        if harness_tools is not None and not isinstance(
            harness_tools, HarnessToolProvider
        ):
            raise WorkflowRuntimeError("harness_tools must be a HarnessToolProvider")
        self._router = router
        self._runner = runner
        self._research_runner = resolved_research_runner
        self._harness_tools = harness_tools
        self._validator = validator
        self._hypothesis_store = hypothesis_store
        self._subagent_lifecycle = subagent_lifecycle
        self._clock = clock
        self._id_factory = id_factory
        # Keep an in-process paused workflow alive so a conversational phase can
        # resume with the complete state produced before its operator review.
        # A process restart does not claim to reconstruct this state.
        self._hypothesis_workflows: dict[str, HypothesisDevelopmentWorkflow] = {}

    @property
    def router(self) -> ModelRouter:
        """Return the composed policy-driven model router."""
        return self._router

    @property
    def runner(self) -> BacktestRunner:
        """Return the composed backtest runner port."""
        return self._runner

    @property
    def research_runner(self) -> ResearchModelRunner:
        """Return the workspace research-model execution port."""
        return self._research_runner

    @property
    def harness_tools(self) -> HarnessToolProvider | None:
        """Return the composed harness MCP tool provider, when configured."""
        return self._harness_tools

    def build_hypothesis_development(
        self,
        *,
        chat: ChatService,
        validator: StrategySandboxValidator | None = None,
        hypothesis_store: HypothesisStore | None = None,
        composer: DevelopmentHypothesisComposer | None = None,
        planner: FalsificationPlanner | None = None,
        rca_composer: RcaComposer | None = None,
        designer: StrategyDesigner | None = None,
        ledger: AttemptLedger | None = None,
        subagent_factory: SubagentContextFactory | None = None,
        journal: CommandJournal | None = None,
        research_role: Role = "research",
        critique_role: Role = "critique",
        coding_role: Role = "coding",
        experiment_id: str | None = None,
        max_rca_cycles: int = DEFAULT_MAX_RCA_CYCLES,
    ) -> HypothesisDevelopmentWorkflow:
        """Compose the falsification-first hypothesis-development workflow."""
        resolved_validator = validator if validator is not None else self._validator
        if resolved_validator is None:
            raise WorkflowRuntimeError(
                "provide a strategy sandbox validator to compose the "
                "hypothesis-development workflow"
            )
        resolved_store = (
            hypothesis_store if hypothesis_store is not None else self._hypothesis_store
        )
        return HypothesisDevelopmentWorkflow(
            chat=chat,
            router=self._router,
            runner=self._runner,
            research_runner=self._research_runner,
            validator=resolved_validator,
            hypothesis_store=resolved_store,
            composer=composer,
            planner=planner,
            rca_composer=rca_composer,
            designer=designer,
            ledger=ledger,
            subagent_factory=subagent_factory,
            journal=journal,
            clock=self._clock,
            id_factory=self._id_factory,
            research_role=research_role,
            critique_role=critique_role,
            coding_role=coding_role,
            experiment_id=experiment_id,
            max_rca_cycles=max_rca_cycles,
            subagent_lifecycle=self._subagent_lifecycle,
        )

    def execute_hypothesis_development(
        self,
        request: HypothesisDevelopmentRequest,
        *,
        chat: ChatService,
        validator: StrategySandboxValidator | None = None,
        hypothesis_store: HypothesisStore | None = None,
        composer: DevelopmentHypothesisComposer | None = None,
        planner: FalsificationPlanner | None = None,
        rca_composer: RcaComposer | None = None,
        designer: StrategyDesigner | None = None,
        ledger: AttemptLedger | None = None,
        subagent_factory: SubagentContextFactory | None = None,
        journal: CommandJournal | None = None,
        research_role: Role = "research",
        critique_role: Role = "critique",
        coding_role: Role = "coding",
        experiment_id: str | None = None,
        max_rca_cycles: int = DEFAULT_MAX_RCA_CYCLES,
    ) -> HypothesisDevelopmentOutcome:
        """Run one launched hypothesis-development request to completion."""
        workflow = self.build_hypothesis_development(
            chat=chat,
            validator=validator,
            hypothesis_store=hypothesis_store,
            composer=composer,
            planner=planner,
            rca_composer=rca_composer,
            designer=designer,
            ledger=ledger,
            subagent_factory=subagent_factory,
            journal=journal,
            research_role=research_role,
            critique_role=critique_role,
            coding_role=coding_role,
            experiment_id=experiment_id,
            max_rca_cycles=max_rca_cycles,
        )
        self._hypothesis_workflows[request.execution_id] = workflow
        outcome = workflow.run(request)
        if outcome.phase.value in {"completed", "failed"}:
            self._hypothesis_workflows.pop(request.execution_id, None)
        return outcome

    def resume_hypothesis_development(
        self,
        request: HypothesisDevelopmentRequest,
        *,
        chat: ChatService,
    ) -> HypothesisDevelopmentOutcome:
        """Run one more hypothesis-creation turn for a resumed execution.

        Called when an operator message arrives for the paused
        hypothesis-creation node.  The runtime rebuilds the workflow over the
        same composed ports and re-enters the conversation; the durable
        transcript is the state, so no checkpoint is required.
        """
        workflow = self._hypothesis_workflows.get(request.execution_id)
        if workflow is None:
            raise WorkflowRuntimeError(
                "paused hypothesis development state is unavailable; "
                "restart resume is not supported yet"
            )
        outcome = workflow.resume(request)
        if outcome.phase.value in {"completed", "failed"}:
            self._hypothesis_workflows.pop(request.execution_id, None)
        return outcome


__all__ = [
    "WorkflowRuntime",
    "WorkflowRuntimeError",
    "build_model_router",
    "build_workflow_runtime",
]
