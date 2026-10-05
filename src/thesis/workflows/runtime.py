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
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Protocol

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
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.session import ToolExecution
from thesis.agent.runtime.store import SubAgentStore
from thesis.agent.skills import SkillRegistry
from thesis.agent.subagents import SubagentContextFactory
from thesis.chat.contracts import SubAgentRecord
from thesis.chat.service import ChatService
from thesis.experiments.ledger import AttemptLedger
from thesis.integrations.harness import HarnessClientPort, ThesisHarnessClient
from thesis.knowledge.service import KnowledgeScope, validate_scope
from thesis.persistence.journal import CommandJournal
from thesis.research.hypothesis import HypothesisStore
from thesis.research.models import ResearchModelStore
from thesis.strategies import WorkspaceStrategyArtifacts
from thesis.strategies.validation import StrategySandboxValidator
from thesis.workflows.backtests import (
    BacktestMode,
    BacktestRunner,
    HarnessBacktestRunner,
)
from thesis.workflows.core.execution_store import ExecutionStore
from thesis.workflows.core.workflow import Detach, Workflow
from thesis.workflows.registry.hypothesis_development import (
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
    build_hypothesis_development_workflow,
)
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ProgramSnapshots,
    ResearchModelRunner,
)


class WorkflowRuntimeError(RuntimeError):
    """Raised when the workflow runtime cannot be composed or executed."""


class RuntimeToolActivity(Protocol):
    """Execute an internal tool through the conversation's activity writer."""

    def __call__(
        self,
        context: ToolContext,
        tool: str,
        arguments: Mapping[str, object],
        execute: Callable[[], ToolExecution],
    ) -> ToolExecution: ...


@dataclass(frozen=True, slots=True)
class WorkflowSessionBindings:
    """Workflow-owned capabilities resolved for one trusted session context."""

    completion_schema: Mapping[str, object] | None = None
    knowledge_scope: KnowledgeScope = "both"
    knowledge_collections: tuple[str, ...] | None = None
    strategy_authoring: WorkspaceStrategyArtifacts | None = None
    programs: ProgramSnapshots | None = None


class WorkflowSessionServices(Protocol):
    """Reusable port between conversation tools and a workflow's policies."""

    def resolve(self, context: ToolContext) -> WorkflowSessionBindings: ...

    def validate_completion(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> str | None: ...

    def apply_completion(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> Mapping[str, object] | None: ...

    def fail(self, record: SubAgentRecord, error: Exception) -> None: ...


def bind_workflow_session_services(
    *,
    runtime: WorkflowRuntime | None,
    chat: ChatService,
    store: SubAgentStore,
    activity: RuntimeToolActivity,
    strategy_artifacts: WorkspaceStrategyArtifacts | None = None,
    programs: ProgramSnapshots | None = None,
    knowledge_scope: KnowledgeScope = "both",
) -> WorkflowSessionServices:
    """Bind session capabilities even when workflow execution is unavailable."""
    if runtime is not None:
        return runtime.bind_session_services(
            chat=chat,
            store=store,
            activity=activity,
            strategy_artifacts=strategy_artifacts,
            programs=programs,
        )
    from thesis.workflows.registry.hypothesis_development.session_services import (
        HypothesisSessionServices,
    )

    return HypothesisSessionServices(
        chat=chat,
        store=store,
        harness_tools=None,
        activity=activity,
        strategy_artifacts=strategy_artifacts,
        programs=programs,
        knowledge_scope=knowledge_scope,
    )


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
    research_runner: ResearchModelRunner | None = None,
    programs: ProgramSnapshots | None = None,
    bundle: PolicyBundle | None = None,
    bundle_hashes: Mapping[str, str] | None = None,
    backtest_mode: BacktestMode = "local",
    metadata: Mapping[str, object] | None = None,
    conversations: Conversations | None = None,
    execution_store: ExecutionStore | None = None,
    detach: Detach | None = None,
    clock: Callable[[], datetime] = _utc_now,
    id_factory: Callable[[str], str] = _new_id,
    invocation_id_factory: Callable[[str], str] | None = None,
    knowledge_scope: KnowledgeScope = "both",
) -> WorkflowRuntime:
    """Compose the model router and backtest runner into one runtime holder.

    A :class:`HarnessBacktestRunner` is built over the injected harness client
    unless an explicit ``runner`` is supplied, so tests can replace the harness
    with a fake while exercising the same runtime surface.  A
    :class:`~thesis.agent.harness_tools.HarnessToolProvider` supplied here is
    held for composition consumers; tool dispatch happens through the unified
    session registry.

    ``research_model_store`` feeds the default
    :class:`~thesis.workflows.research_models.BacktestResearchModelRunner` and
    therefore applies only when ``research_runner`` is omitted; supplying it
    beside an explicit runner is a composition error, never a silent ignore.
    """

    if research_runner is not None and research_model_store is not None:
        raise WorkflowRuntimeError(
            "research_model_store applies only when research_runner is omitted"
        )

    if invocation_id_factory is not None:

        def _invocation_id() -> str:
            return invocation_id_factory("invocation")

    else:

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
        invocation_id_factory=_invocation_id,
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
        )
    )
    return WorkflowRuntime(
        router=router,
        runner=resolved_runner,
        research_runner=resolved_research_runner,
        programs=programs,
        harness_tools=harness_tools,
        validator=strategy_validator,
        hypothesis_store=hypothesis_store,
        conversations=conversations,
        execution_store=execution_store,
        detach=detach,
        clock=clock,
        id_factory=id_factory,
        knowledge_scope=knowledge_scope,
    )


class WorkflowRuntime:
    """The slim composition root: composed ports plus workflow facades.

    The runtime owns the composed :class:`ModelRouter` and
    :class:`BacktestRunner` and exposes them to every workflow.  The
    hypothesis-named ``build_*``/``execute_*`` members below are compatibility
    facades over the concrete package assembled under
    ``thesis.workflows.registry.hypothesis_development``; the workflow's spec,
    executors, and state machine live there, not here.
    """

    def __init__(
        self,
        *,
        router: ModelRouter,
        runner: BacktestRunner,
        research_runner: ResearchModelRunner | None = None,
        programs: ProgramSnapshots | None = None,
        harness_tools: HarnessToolProvider | None = None,
        validator: StrategySandboxValidator | None = None,
        hypothesis_store: HypothesisStore | None = None,
        conversations: Conversations | None = None,
        execution_store: ExecutionStore | None = None,
        detach: Detach | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
        knowledge_scope: KnowledgeScope = "both",
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
        self._programs = programs
        self._harness_tools = harness_tools
        self._validator = validator
        self._hypothesis_store = hypothesis_store
        self._conversations = conversations
        self._execution_store = execution_store
        self._detach = detach
        self._clock = clock
        self._id_factory = id_factory
        self._knowledge_scope = validate_scope(knowledge_scope)
        self._workflows: dict[
            ChatService,
            Workflow[
                HypothesisDevelopmentRequest,
                HypothesisDevelopmentState,
                HypothesisDevelopmentOutcome,
            ],
        ] = {}
        self._workflow_lock = RLock()

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

    @property
    def execution_store(self) -> ExecutionStore | None:
        """Return the composed execution store, when configured."""
        return self._execution_store

    def build_hypothesis_development(
        self,
        *,
        chat: ChatService,
        validator: StrategySandboxValidator | None = None,
        hypothesis_store: HypothesisStore | None = None,
        ledger: AttemptLedger | None = None,
        subagent_factory: SubagentContextFactory | None = None,
        journal: CommandJournal | None = None,
        research_role: Role = "research",
        experiment_id: str | None = None,
    ) -> Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        HypothesisDevelopmentOutcome,
    ]:
        """Reuse the default workflow per chat; explicit overrides build separately."""
        if (
            any(
                value is not None
                for value in (
                    validator,
                    hypothesis_store,
                    ledger,
                    subagent_factory,
                    journal,
                    experiment_id,
                )
            )
            or research_role != "research"
        ):
            return self._build_hypothesis_development(
                chat=chat,
                validator=validator,
                hypothesis_store=hypothesis_store,
                ledger=ledger,
                subagent_factory=subagent_factory,
                journal=journal,
                research_role=research_role,
                experiment_id=experiment_id,
            )
        with self._workflow_lock:
            if chat not in self._workflows:
                self._workflows[chat] = self._build_hypothesis_development(chat=chat)
            return self._workflows[chat]

    def bind_session_services(
        self,
        *,
        chat: ChatService,
        store: SubAgentStore,
        activity: RuntimeToolActivity,
        strategy_artifacts: WorkspaceStrategyArtifacts | None = None,
        programs: ProgramSnapshots | None = None,
    ) -> WorkflowSessionServices:
        """Bind workflow-owned policy and completion services to conversation ports."""
        from thesis.workflows.registry.hypothesis_development.session_services import (
            HypothesisSessionServices,
        )

        return HypothesisSessionServices(
            chat=chat,
            store=store,
            workflow=lambda: self.build_hypothesis_development(chat=chat),
            harness_tools=self._harness_tools,
            activity=activity,
            strategy_artifacts=strategy_artifacts,
            programs=self._programs if programs is None else programs,
        )

    def _build_hypothesis_development(
        self,
        *,
        chat: ChatService,
        validator: StrategySandboxValidator | None = None,
        hypothesis_store: HypothesisStore | None = None,
        ledger: AttemptLedger | None = None,
        subagent_factory: SubagentContextFactory | None = None,
        journal: CommandJournal | None = None,
        research_role: Role = "research",
        experiment_id: str | None = None,
    ) -> Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        HypothesisDevelopmentOutcome,
    ]:
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
        return build_hypothesis_development_workflow(
            chat=chat,
            router=self._router,
            runner=self._runner,
            research_runner=self._research_runner,
            programs=self._programs,
            validator=resolved_validator,
            hypothesis_store=resolved_store,
            ledger=ledger,
            subagent_factory=subagent_factory,
            journal=journal,
            clock=self._clock,
            id_factory=self._id_factory,
            research_role=research_role,
            experiment_id=experiment_id,
            conversations=self._conversations,
            execution_store=self._execution_store,
            detach=self._detach,
            knowledge_scope=self._knowledge_scope,
        )


__all__ = [
    "RuntimeToolActivity",
    "WorkflowRuntime",
    "WorkflowRuntimeError",
    "WorkflowSessionBindings",
    "WorkflowSessionServices",
    "build_model_router",
    "bind_workflow_session_services",
    "build_workflow_runtime",
]
