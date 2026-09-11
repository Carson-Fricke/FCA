"""Compose the Thesis workflow runtime from injected ports.

This module is the composition root for the Thesis workflow verticals.  It
builds the policy-driven :class:`~thesis.agent.router.ModelRouter` from role
policies, model profiles, and a :class:`~thesis.agent.skills.SkillRegistry`,
and builds a :class:`~thesis.workflows.backtests.HarnessBacktestRunner` over
the published :class:`~thesis.integrations.harness.ThesisHarnessClient`
boundary.  The resulting :class:`WorkflowRuntime` holder constructs and
executes the hypothesis-validation and strategy-development workflows against
those composed ports.

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
from datetime import UTC, datetime, timedelta

from thesis.agent.harness_tools import (
    HarnessToolProvider,
    HarnessToolResult,
    register_harness_mcp_skill,
)
from thesis.agent.router import (
    ModelProfile,
    ModelProvider,
    ModelRouter,
    PolicyBundle,
    Role,
    RolePolicy,
)
from thesis.agent.skills import SkillRegistry
from thesis.agent.tool_dispatch import HarnessToolCall, HarnessToolDispatcher
from thesis.artifacts.importer import ArtifactImporter
from thesis.chat.service import ChatService
from thesis.integrations.harness import HarnessClientPort, ThesisHarnessClient
from thesis.persistence.journal import CommandJournal
from thesis.strategies.validation import StrategySandboxValidator
from thesis.workflows.backtests import (
    BacktestMode,
    BacktestRunner,
    HarnessBacktestRunner,
)
from thesis.workflows.hypothesis_validation import (
    DEFAULT_LOOKBACK,
    DataReadinessProbe,
    HypothesisComposer,
    HypothesisValidationOutcome,
    HypothesisValidationRequest,
    HypothesisValidationWorkflow,
)
from thesis.workflows.strategy_development import (
    StrategyComposer,
    StrategyDevelopmentOutcome,
    StrategyDevelopmentRequest,
    StrategyDevelopmentWorkflow,
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
    bundle: PolicyBundle | None = None,
    bundle_hashes: Mapping[str, str] | None = None,
    backtest_mode: BacktestMode = "local",
    metadata: Mapping[str, object] | None = None,
    clock: Callable[[], datetime] = _utc_now,
    id_factory: Callable[[str], str] = _new_id,
    invocation_id_factory: Callable[[], str] | None = None,
) -> WorkflowRuntime:
    """Compose the model router and backtest runner into one runtime holder.

    A :class:`HarnessBacktestRunner` is built over the injected harness client
    unless an explicit ``runner`` is supplied, so tests can replace the harness
    with a fake while exercising the same runtime surface.  When a
    :class:`~thesis.agent.harness_tools.HarnessToolProvider` is supplied, the
    runtime also exposes a policy-scoped
    :class:`~thesis.agent.tool_dispatch.HarnessToolDispatcher`.
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
    return WorkflowRuntime(
        router=router,
        runner=resolved_runner,
        harness_tools=harness_tools,
        clock=clock,
        id_factory=id_factory,
    )


class WorkflowRuntime:
    """Holder that builds and executes Thesis workflows over composed ports.

    The runtime owns the composed :class:`ModelRouter` and
    :class:`BacktestRunner`.  Each builder pairs them with an injected
    :class:`ChatService` (plus the workflow-specific validator, composer, probe,
    journal, and importer ports) and returns a runnable workflow.  The matching
    ``execute_*`` helpers run an already-launched request through a freshly
    composed workflow.
    """

    def __init__(
        self,
        *,
        router: ModelRouter,
        runner: BacktestRunner,
        harness_tools: HarnessToolProvider | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
    ) -> None:
        if not isinstance(router, ModelRouter):
            raise WorkflowRuntimeError("router must be a ModelRouter")
        if not callable(getattr(runner, "run", None)):
            raise WorkflowRuntimeError("runner must implement the BacktestRunner port")
        if harness_tools is not None and not isinstance(
            harness_tools, HarnessToolProvider
        ):
            raise WorkflowRuntimeError("harness_tools must be a HarnessToolProvider")
        self._router = router
        self._runner = runner
        self._harness_tools = harness_tools
        self._tool_dispatcher = (
            None
            if harness_tools is None
            else HarnessToolDispatcher(router, harness_tools)
        )
        self._clock = clock
        self._id_factory = id_factory

    @property
    def router(self) -> ModelRouter:
        """Return the composed policy-driven model router."""
        return self._router

    @property
    def runner(self) -> BacktestRunner:
        """Return the composed backtest runner port."""
        return self._runner

    @property
    def harness_tools(self) -> HarnessToolProvider | None:
        """Return the composed harness MCP tool provider, when configured."""
        return self._harness_tools

    @property
    def tool_dispatcher(self) -> HarnessToolDispatcher | None:
        """Return the policy-scoped harness tool dispatcher, when configured."""
        return self._tool_dispatcher

    def dispatch_tool(
        self,
        role: Role | str,
        call: HarnessToolCall | str,
        arguments: Mapping[str, object] | None = None,
    ) -> HarnessToolResult:
        """Dispatch a model tool request through the harness MCP provider."""
        if self._tool_dispatcher is None:
            raise WorkflowRuntimeError(
                "provide a harness tool provider to dispatch tool calls"
            )
        return self._tool_dispatcher.dispatch(role, call, arguments)

    def build_hypothesis_validation(
        self,
        *,
        chat: ChatService,
        composer: HypothesisComposer | None = None,
        probe: DataReadinessProbe | None = None,
        journal: CommandJournal | None = None,
        role: Role = "research",
        experiment_id: str | None = None,
        lookback: timedelta = DEFAULT_LOOKBACK,
    ) -> HypothesisValidationWorkflow:
        """Compose the hypothesis-validation workflow over the runtime ports."""
        return HypothesisValidationWorkflow(
            chat=chat,
            router=self._router,
            runner=self._runner,
            composer=composer,
            probe=probe,
            journal=journal,
            clock=self._clock,
            id_factory=self._id_factory,
            role=role,
            experiment_id=experiment_id,
            lookback=lookback,
        )

    def build_strategy_development(
        self,
        *,
        chat: ChatService,
        validator: StrategySandboxValidator,
        composer: StrategyComposer | None = None,
        importer: ArtifactImporter | None = None,
        journal: CommandJournal | None = None,
        role: Role = "coding",
        experiment_id: str | None = None,
    ) -> StrategyDevelopmentWorkflow:
        """Compose the strategy-development workflow over the runtime ports."""
        return StrategyDevelopmentWorkflow(
            chat=chat,
            router=self._router,
            validator=validator,
            runner=self._runner,
            composer=composer,
            importer=importer,
            journal=journal,
            clock=self._clock,
            id_factory=self._id_factory,
            role=role,
            experiment_id=experiment_id,
        )

    def execute_hypothesis_validation(
        self,
        request: HypothesisValidationRequest,
        *,
        chat: ChatService,
        composer: HypothesisComposer | None = None,
        probe: DataReadinessProbe | None = None,
        journal: CommandJournal | None = None,
        role: Role = "research",
        experiment_id: str | None = None,
        lookback: timedelta = DEFAULT_LOOKBACK,
    ) -> HypothesisValidationOutcome:
        """Run one launched hypothesis-validation request to completion."""
        workflow = self.build_hypothesis_validation(
            chat=chat,
            composer=composer,
            probe=probe,
            journal=journal,
            role=role,
            experiment_id=experiment_id,
            lookback=lookback,
        )
        return workflow.run(request)

    def execute_strategy_development(
        self,
        request: StrategyDevelopmentRequest,
        *,
        chat: ChatService,
        validator: StrategySandboxValidator,
        composer: StrategyComposer | None = None,
        importer: ArtifactImporter | None = None,
        journal: CommandJournal | None = None,
        role: Role = "coding",
        experiment_id: str | None = None,
    ) -> StrategyDevelopmentOutcome:
        """Run one launched strategy-development request to completion."""
        workflow = self.build_strategy_development(
            chat=chat,
            validator=validator,
            composer=composer,
            importer=importer,
            journal=journal,
            role=role,
            experiment_id=experiment_id,
        )
        return workflow.run(request)


__all__ = [
    "WorkflowRuntime",
    "WorkflowRuntimeError",
    "build_model_router",
    "build_workflow_runtime",
]
