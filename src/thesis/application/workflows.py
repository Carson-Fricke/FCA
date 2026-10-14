"""Compose the workflow runtime over explicit model and execution ports."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from threading import RLock
from typing import cast

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
from thesis.agent.skills import SkillRegistry
from thesis.extensions.workflows.bindings import (
    RuntimeToolActivity,
    WorkflowSessionBindings,
    WorkflowSessionServices,
)
from thesis.extensions.workflows.hypothesis_development import (
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
    build_hypothesis_development_workflow,
)
from thesis.knowledge.service import KnowledgeScope, validate_scope
from thesis.research.hypothesis import HypothesisStore
from thesis.research.models import ResearchModelStore
from thesis.workflows.backtests import (
    BacktestRunner,
)
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ProgramSnapshots,
    ResearchModelRunner,
)
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.execution.worker import WorkStore
from thesis_runtime.subagents.store import SubAgentStore
from thesis_runtime.workflows.execution_store import ExecutionStore
from thesis_runtime.workflows.registry import WorkflowRegistration
from thesis_runtime.workflows.workflow import Detach, Workflow, WorkflowPorts


class WorkflowRuntimeError(RuntimeError):
    """Raised when the workflow runtime cannot be composed or executed."""


def bind_workflow_session_services(
    *,
    runtime: WorkflowRuntime | None,
    chat: ChatService,
    store: SubAgentStore,
    activity: RuntimeToolActivity,
    programs: ProgramSnapshots | None = None,
    knowledge_scope: KnowledgeScope = "both",
) -> WorkflowSessionServices:
    """Bind session capabilities even when workflow execution is unavailable."""
    if runtime is not None:
        return runtime.bind_session_services(
            chat=chat,
            store=store,
            activity=activity,
            programs=programs,
        )
    from thesis.extensions.workflows.hypothesis_development.session_services import (
        HypothesisSessionServices,
    )

    return HypothesisSessionServices(
        chat=chat,
        store=store,
        harness_tools=None,
        activity=activity,
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


def build_workflow_runtime(
    *,
    role_policies: Mapping[Role, RolePolicy],
    model_profiles: Mapping[str, ModelProfile],
    providers: Mapping[str, ModelProvider],
    skills: SkillRegistry,
    runner: BacktestRunner | None = None,
    harness_tools: HarnessToolProvider | None = None,
    hypothesis_store: HypothesisStore | None = None,
    research_model_store: ResearchModelStore | None = None,
    research_runner: ResearchModelRunner | None = None,
    programs: ProgramSnapshots | None = None,
    bundle: PolicyBundle | None = None,
    bundle_hashes: Mapping[str, str] | None = None,
    conversations: Conversations | None = None,
    execution_store: ExecutionStore | None = None,
    detach: Detach | None = None,
    work_store: WorkStore | None = None,
    clock: Callable[[], datetime] = _utc_now,
    id_factory: Callable[[str], str] = _new_id,
    invocation_id_factory: Callable[[str], str] | None = None,
    knowledge_scope: KnowledgeScope = "both",
) -> WorkflowRuntime:
    """Compose the model router and backtest runner into one runtime holder.

    Callers provide an execution runner. A
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
    if runner is None:
        raise WorkflowRuntimeError("provide a backtest runner")
    resolved_runner = runner
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
        hypothesis_store=hypothesis_store,
        conversations=conversations,
        execution_store=execution_store,
        detach=detach,
        work_store=work_store,
        clock=clock,
        id_factory=id_factory,
        knowledge_scope=knowledge_scope,
    )


class WorkflowRuntime:
    """The slim composition root: composed ports plus workflow facades.

    The runtime owns the composed :class:`ModelRouter` and
    :class:`BacktestRunner` and exposes them to every workflow.  The
    hypothesis-named ``build_*``/``execute_*`` members below compose the
    concrete package under
    ``thesis.extensions.workflows.hypothesis_development``; the workflow's spec,
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
        hypothesis_store: HypothesisStore | None = None,
        conversations: Conversations | None = None,
        execution_store: ExecutionStore | None = None,
        detach: Detach | None = None,
        work_store: WorkStore | None = None,
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
        self._hypothesis_store = hypothesis_store
        self._conversations = conversations
        self._execution_store = execution_store
        self._detach = detach
        self._work_store = work_store
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
        self._research_registrations: dict[ChatService, WorkflowRegistration] = {}

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
        hypothesis_store: HypothesisStore | None = None,
        experiment_id: str | None = None,
    ) -> Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        HypothesisDevelopmentOutcome,
    ]:
        """Reuse the default workflow per chat; explicit overrides build separately."""
        if hypothesis_store is not None or experiment_id is not None:
            return self._build_hypothesis_development(
                chat=chat,
                hypothesis_store=hypothesis_store,
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
        programs: ProgramSnapshots | None = None,
    ) -> WorkflowSessionServices:
        """Bind workflow-owned policy and completion services to conversation ports."""
        from thesis.extensions.workflows.hypothesis_development.endpoint import (
            HypothesisExtension,
        )
        from thesis.extensions.workflows.hypothesis_development.session_services import (
            HypothesisSessionServices,
        )

        hypothesis = HypothesisSessionServices(
            chat=chat,
            store=store,
            workflow=lambda: self.build_hypothesis_development(chat=chat),
            extension=cast(
                "HypothesisExtension",
                self.build_hypothesis_development(chat=chat).extension,
            ),
            harness_tools=self._harness_tools,
            activity=activity,
            programs=self._programs if programs is None else programs,
        )
        if self._conversations is None or self._execution_store is None:
            return hypothesis
        from thesis.extensions.workflows.research_investigation.runtime import (
            ResearchInvestigationRuntime,
        )
        from thesis.extensions.workflows.session_services import WorkflowSessionRouter

        research = ResearchInvestigationRuntime(
            ports=WorkflowPorts(
                chat,
                self._clock,
                self._id_factory,
                conversations=self._conversations,
                execution_store=self._execution_store,
                detach=self._detach,
                work_store=self._work_store,
            ),
            programs=self._programs if programs is None else programs,
            observe_submission=hypothesis.record_delegated_submission,
            knowledge_scope=self._knowledge_scope,
        )
        self._research_registrations[chat] = research.registration
        return WorkflowSessionRouter(chat, hypothesis, research)

    def research_registration(
        self, *, chat: ChatService
    ) -> WorkflowRegistration | None:
        """Return the child workflow registration bound with session capabilities."""
        return self._research_registrations.get(chat)

    def _build_hypothesis_development(
        self,
        *,
        chat: ChatService,
        hypothesis_store: HypothesisStore | None = None,
        experiment_id: str | None = None,
    ) -> Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        HypothesisDevelopmentOutcome,
    ]:
        """Compose the falsification-first hypothesis-development workflow."""
        resolved_store = (
            hypothesis_store if hypothesis_store is not None else self._hypothesis_store
        )
        return build_hypothesis_development_workflow(
            chat=chat,
            runner=self._runner,
            research_runner=self._research_runner,
            programs=self._programs,
            hypothesis_store=resolved_store,
            clock=self._clock,
            id_factory=self._id_factory,
            experiment_id=experiment_id,
            conversations=self._conversations,
            execution_store=self._execution_store,
            detach=self._detach,
            work_store=self._work_store,
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
