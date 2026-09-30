"""The serving composition: chat, sub-agents, and the workflow runtime.

One implementation backs both `thesis serve` (the deployed container) and the
local API script.  Given a durable chat service and a model provider, it
composes the conversations service, the Thesis control tools, the workflow
runtime, and the workflow launcher that the HTTP app serves.

The workflow runtime needs a harness MCP tool provider; without one the stack
still composes chat and sub-agents but leaves the workflow runtime unwired and
launches nothing.  `thesis serve` composes it from the environment:

- ``OPENROUTER_API_KEY`` (with ``THESIS_MODEL``) enables the model provider.
- ``HARNESS_MCP_URL`` (with ``HARNESS_API_BEARER_TOKEN``) enables the harness
  tool provider and therefore the workflow runtime.
- ``THESIS_COSMOS_ENDPOINT``/``THESIS_COSMOS_DATABASE`` already back the chat
  service; the execution store reads the same settings.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from thesis.agent.context import PromptContext
from thesis.agent.harness_tools import (
    HARNESS_TOOL_NAMES,
    HarnessToolProvider,
    open_harness_mcp_composition,
    open_harness_tool_provider,
)
from thesis.agent.hypothesis_skills import (
    with_hypothesis_development_skills,
)
from thesis.agent.router import (
    ModelProfile,
    ModelResponse,
    Role,
    RolePolicy,
)
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.control import ControlToolService
from thesis.agent.runtime.conversation import (
    Conversations,
    ReplyChunks,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import (
    OperatorPolicyRegistry,
    SpawnPolicy,
)
from thesis.agent.runtime.registry import (
    THESIS_TOOL_NAMES,
    build_session_registry,
)
from thesis.agent.runtime.session import SessionToolRegistry
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.agent.runtime.workflow_tools import WorkflowLauncher
from thesis.agent.session_reply import SessionReplyProvider
from thesis.agent.skills import SkillRegistry
from thesis.agent.tool_dispatch import with_harness_mcp
from thesis.chat.contracts import (
    ChatMessage,
    MessageRole,
    RecordNotFound,
    SubAgentRecord,
)
from thesis.chat.responder import DevelopmentRuntime
from thesis.chat.service import ChatService
from thesis.chat.streaming import InMemoryChatStreamBroker
from thesis.chat.workflow_launcher import (
    Detach,
    RuntimeWorkflowLauncher,
    daemon_thread,
)
from thesis.integrations.mcp import (
    HarnessMcpError,
    harness_mcp_topology_from_env,
)
from thesis.providers.openrouter import (
    OpenRouterProfile,
    OpenRouterProvider,
)
from thesis.providers.transports import (
    DEFAULT_SYSTEM_PROMPT,
    EnvironmentApiKeySource,
    HttpOpenRouterTransport,
    openrouter_reply_provider,
)
from thesis.research.hypothesis import (
    HypothesisStore,
    WorkspaceHypothesisStore,
)
from thesis.research.models import (
    ResearchModelStore,
    WorkspaceResearchModelStore,
)
from thesis.strategies.validation import (
    StrategyValidationRequest,
    StrategyValidationResult,
)
from thesis.workflows.core.execution_store import ExecutionStore
from thesis.workflows.local import McpBacktestRunner, McpStudyRunner
from thesis.workflows.registry.hypothesis_development import (
    HypothesisDevelopmentRequest,
)
from thesis.workflows.registry.hypothesis_development.composers.hypothesis import (
    _compose_research_model,
)
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
)
from thesis.workflows.runtime import (
    WorkflowRuntime,
    build_workflow_runtime,
)

#: Default model when ``THESIS_MODEL`` is unset.
DEFAULT_SERVING_MODEL = "deepseek/deepseek-v4.1-flash"

#: The model profile every serving role resolves to.
_SERVING_PROFILE_ID = "serving-openrouter"


class _WorkflowModelProvider:
    """Adapt the OpenRouter chat client to the router's provider port."""

    def __init__(self, provider: OpenRouterProvider) -> None:
        self._provider = provider

    def invoke(
        self,
        *,
        profile: ModelProfile,
        prompt: PromptContext,
        allowed_tools: tuple[str, ...],
        response_format: Mapping[str, object] | None = None,
    ) -> ModelResponse:
        del profile, allowed_tools
        from thesis.providers.invocations import ChatMessage as ProviderChatMessage

        completion = self._provider.complete(
            messages=[ProviderChatMessage(role="user", content=prompt.rendered)],
            response_format=response_format,
        )
        return ModelResponse(output=completion.content)


class _StructuralStrategyValidator:
    """Accept a structurally valid bundle for serving composition."""

    def validate(self, request: StrategyValidationRequest) -> StrategyValidationResult:
        return StrategyValidationResult(
            True, (), validation_id=f"serving-{request.bundle_sha256[:16]}"
        )


@dataclass(frozen=True, slots=True)
class ServingStack:
    """The composed services `create_app` and the HTTP routes consume."""

    chat_service: ChatService
    broker: InMemoryChatStreamBroker
    conversations: Conversations | None
    workflow_runtime: WorkflowRuntime | None
    harness_tool_provider: HarnessToolProvider | None
    operator_policies: OperatorPolicyRegistry


def _workspace_stores(
    workspace_root: str | Path | None,
) -> tuple[ResearchModelStore | None, HypothesisStore | None]:
    """Bind durable research stores to a workspace tree, or keep them in memory.

    A deployed container has no Azure workspace path provider yet, so a
    composition without ``workspace_root`` keeps model publications and
    hypothesis artifacts in process memory.  Runs, trials,
    and conversations stay durable in the execution store and Cosmos; only the
    workspace-side artifacts are ephemeral until the Azure workspace persists
    that tree.
    """
    if workspace_root is None:
        return None, None
    from thesis.workspaces.local import LocalWorkspaceProvider

    provider = LocalWorkspaceProvider(str(workspace_root))
    return (
        WorkspaceResearchModelStore(provider),
        WorkspaceHypothesisStore(str(workspace_root)),
    )


def build_serving_workflow_runtime(
    model_provider: OpenRouterProvider,
    harness_tools: HarnessToolProvider,
    *,
    conversations: Conversations | None = None,
    execution_store: ExecutionStore | None = None,
    workspace_root: str | Path | None = None,
    detach: Detach | None = daemon_thread,
) -> WorkflowRuntime:
    """Compose the falsification-first workflow runtime for the serving roles."""
    skills = SkillRegistry()
    profiles = {
        _SERVING_PROFILE_ID: ModelProfile(
            profile_id=_SERVING_PROFILE_ID,
            version="1.0.0",
            provider="openrouter",
            model=_serving_model_name(),
        )
    }

    def policy(role: Role) -> RolePolicy:
        base = RolePolicy(
            role=role,
            version="1.0.0",
            model_profile=_SERVING_PROFILE_ID,
            allowed_tools=(),
        )
        return with_hypothesis_development_skills(with_harness_mcp(base))

    models, hypothesis_store = _workspace_stores(workspace_root)
    runner = McpBacktestRunner(harness_tools)
    # Research models always compile to a ``study`` shell, so the falsification
    # phase executes through ``run_study``; the backtest runner would reject
    # the study configuration before any computation.
    research_runner = BacktestResearchModelRunner(
        McpStudyRunner(harness_tools),
        models=models,
    )
    return build_workflow_runtime(
        role_policies={
            "research": policy("research"),
            "critique": policy("critique"),
            "coding": policy("coding"),
        },
        model_profiles=profiles,
        providers={"openrouter": _WorkflowModelProvider(model_provider)},
        skills=skills,
        runner=runner,
        research_runner=research_runner,
        strategy_validator=_StructuralStrategyValidator(),
        hypothesis_store=hypothesis_store,
        conversations=conversations,
        execution_store=execution_store,
        detach=detach,
    )


def _serving_model_name() -> str:
    """Return the configured model, defaulting to the serving model."""
    return (os.environ.get("THESIS_MODEL") or "").strip() or DEFAULT_SERVING_MODEL


def open_serving_model_provider() -> OpenRouterProvider | None:
    """Open the OpenRouter provider when a model key is configured.

    `thesis serve` treats the model key as optional: without it the app is a
    durable message surface, matching the local composition's fail-closed
    behavior instead of failing every reply.
    """
    if not (os.environ.get("OPENROUTER_API_KEY") or "").strip():
        return None
    profile = OpenRouterProfile(
        profile_id=_SERVING_PROFILE_ID,
        model=_serving_model_name(),
    )
    return OpenRouterProvider(
        profile,
        EnvironmentApiKeySource(),
        HttpOpenRouterTransport(profile.base_url),
    )


def open_serving_harness_tools() -> HarnessToolProvider | None:
    """Open the harness MCP tool provider when a harness URL is configured.

    A deployed container has no local harness process, so only an explicit
    ``HARNESS_MCP_URL`` enables the provider; an unreachable harness logs and
    leaves the workflow runtime unwired rather than failing startup.
    """
    if not (os.environ.get("HARNESS_MCP_URL") or "").strip():
        return None
    try:
        composition = open_harness_mcp_composition(harness_mcp_topology_from_env())
    except (HarnessMcpError, OSError) as error:
        print(
            f"harness MCP tool catalog unavailable; the workflow runtime is "
            f"not composed: {error}",
            file=sys.stderr,
        )
        return None
    return open_harness_tool_provider(client=composition.client)


def validate_hypothesis_study_contract(
    harness_tools: HarnessToolProvider | None,
    phase: str | None,
    completion: Mapping[str, object],
) -> str | None:
    """Compile the authored study program before the contract is applied.

    Only the hypothesis-creation contract carries an authored program.  A build
    failure is returned as the repair reason, so the phase agent fixes its
    source inside the same turn instead of the trials phase failing the whole
    execution after acceptance.  Shape errors are left to the contract
    validator, and a harness outage never blocks the completion — the run
    surfaces that failure on its own.
    """
    if harness_tools is None or phase != "hypothesis_creation":
        return None
    payload = completion.get("hypothesis")
    if not isinstance(payload, Mapping):
        return None
    model_payload = payload.get("model")
    if not isinstance(model_payload, Mapping):
        return None
    try:
        _compose_research_model(model_payload)
    except Exception:  # noqa: BLE001 - the contract validator reports shape
        return None
    try:
        # The harness builds the runtime study job from the model document and
        # compiles the authored program; no composition-side envelope is needed.
        result = harness_tools.validate_study(model=dict(model_payload))
    except Exception:  # noqa: BLE001 - validation is advisory to the turn
        return None
    if result.ok:
        return None
    detail = result.text or ("" if result.failure is None else result.failure.message)
    return (
        "the authored study program does not compile in the harness; fix "
        "model.source and call request_phase_completion again: "
        f"{detail or 'unknown build failure'}"
    )


def build_serving_stack(
    *,
    chat_service: ChatService,
    model_provider: OpenRouterProvider | None = None,
    harness_tools: HarnessToolProvider | None = None,
    execution_store: ExecutionStore | None = None,
    workspace_root: str | Path | None = None,
    detach: Detach | None = daemon_thread,
    policies: OperatorPolicyRegistry | None = None,
    control_factory: Callable[..., ControlToolService] = ControlToolService,
    on_turn_error: Callable[[tuple[str, str, Exception]], None] | None = None,
) -> ServingStack:
    """Compose the serving stack over one durable chat service.

    With a ``model_provider`` the conversations service, control tools,
    launcher, and (when ``harness_tools`` is present) the workflow runtime are
    composed.  Without one the stack is chat-only: conversations stay ``None``
    and ``create_app`` serves the durable message surface without replies.
    """
    resolved_policies = policies if policies is not None else OperatorPolicyRegistry()
    broker = InMemoryChatStreamBroker()
    if model_provider is None:
        return ServingStack(
            chat_service=chat_service,
            broker=broker,
            conversations=None,
            workflow_runtime=None,
            harness_tool_provider=None,
            operator_policies=resolved_policies,
        )

    subagent_store = PersistenceSubAgentStore(chat_service.persistence)
    # Permissive by default: undeclared roles are allowed, children inherit the
    # caller's tools, and root spawning is authorized. Only escalation and the
    # root budget ceiling are enforced.
    spawn_policy = SpawnPolicy()
    lifecycle = SubAgentLifecycle(
        chat_service,
        store=subagent_store,
        max_depth=spawn_policy.max_depth,
        max_children=spawn_policy.max_children,
    )
    # The closures below late-bind the control service and workflow runtime
    # composed from the conversations service they are handed to.
    holder: dict[str, object] = {
        "control_service": None,
        "workflow_runtime": None,
        "launcher": None,
    }
    allowed_tools: tuple[str, ...] = tuple(
        sorted({*HARNESS_TOOL_NAMES, *THESIS_TOOL_NAMES})
    )

    def execution_epoch(workspace_id: str, execution_id: str) -> int | None:
        try:
            return chat_service.get_execution(workspace_id, execution_id).intent_epoch
        except RecordNotFound:
            return None

    def apply_phase_completion(
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        runtime = cast("WorkflowRuntime | None", holder["workflow_runtime"])
        if runtime is None or record.workflow_execution_id is None:
            return None
        execution = chat_service.get_execution(
            workspace_id, record.workflow_execution_id
        )
        request = HypothesisDevelopmentRequest.from_execution(chat_service, execution)
        workflow = runtime.build_hypothesis_development(chat=chat_service)
        return workflow.apply_phase_completion(request, record.sub_agent_id, completion)

    def phase_completion_schema(context: ToolContext) -> Mapping[str, object] | None:
        runtime = cast("WorkflowRuntime | None", holder["workflow_runtime"])
        if (
            runtime is None
            or not context.workflow_execution_id
            or not context.sub_agent_id
        ):
            return None
        record = subagent_store.get_sub_agent(
            context.workspace_id, context.sub_agent_id
        )
        if record is None or not record.phase or not record.workflow_execution_id:
            return None
        workflow = runtime.build_hypothesis_development(chat=chat_service)
        return workflow.spec.completion_schemas.get(record.phase)

    def registry_for(context: ToolContext) -> SessionToolRegistry:
        launcher = holder["launcher"]
        return build_session_registry(
            context=context,
            harness_tools=harness_tools,
            control=cast("ControlToolService", holder["control_service"]),
            workflow=cast("WorkflowLauncher", launcher)
            if launcher is not None
            else None,
            spawn_policy=spawn_policy,
            phase_completion_schema=phase_completion_schema(context),
        )

    def fail_phase_execution(record: SubAgentRecord, error: Exception) -> None:
        runtime = cast("WorkflowRuntime | None", holder["workflow_runtime"])
        if runtime is None or record.workflow_execution_id is None:
            return
        execution = chat_service.get_execution(
            record.workspace_id, record.workflow_execution_id
        )
        request = HypothesisDevelopmentRequest.from_execution(chat_service, execution)
        workflow = runtime.build_hypothesis_development(chat=chat_service)
        workflow.fail_execution(request, reason=str(error) or type(error).__name__)

    def validate_phase_contract(
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> str | None:
        del workspace_id
        return validate_hypothesis_study_contract(
            harness_tools, record.phase, completion
        )

    def trigger_message_id(history: Sequence[ChatMessage]) -> str | None:
        for message in reversed(history):
            if message.role is MessageRole.USER:
                return message.message_id
        return None

    def provider_for(
        workspace_id: str, chat_id: str, history: Sequence[ChatMessage]
    ) -> SessionReplyProvider:
        context = ToolContext(
            workspace_id=workspace_id,
            chat_id=chat_id,
            conversation_id=chat_id,
            actor_id="thesis-agent",
            trigger_message_id=trigger_message_id(history),
            allowed_tools=allowed_tools,
        )
        return SessionReplyProvider(
            workspace_id=workspace_id,
            chat_id=chat_id,
            conversation_id=chat_id,
            provider=model_provider,
            registry=registry_for(context),
            store=subagent_store,
        )

    def reply_source(
        workspace_id: str, chat_id: str, history: Sequence[ChatMessage]
    ) -> ReplyChunks:
        if harness_tools is None:
            return openrouter_reply_provider(
                model_provider, system_prompt=DEFAULT_SYSTEM_PROMPT
            )(history)
        return provider_for(workspace_id, chat_id, history)(history)

    # One conversations service owns every conversation's turns and routing;
    # the closures above late-bind the control service and workflow runtime
    # composed from it below.
    conversations = Conversations(
        chat=chat_service,
        broker=broker,
        reply_for=reply_source,
        store=subagent_store,
        provider=model_provider,
        registry_factory=registry_for,
        lifecycle=lifecycle,
        policies=resolved_policies,
        on_fault=fail_phase_execution,
        on_turn_error=on_turn_error,
    )
    control_service = control_factory(
        conversations=conversations,
        execution_epochs=execution_epoch,
        phase_completions=apply_phase_completion,
        contract_validator=validate_phase_contract,
    )
    holder["control_service"] = control_service

    workflow_runtime: WorkflowRuntime | None = None
    if harness_tools is not None:
        workflow_runtime = build_serving_workflow_runtime(
            model_provider,
            harness_tools,
            conversations=conversations,
            execution_store=execution_store,
            workspace_root=workspace_root,
            detach=detach,
        )
        holder["workflow_runtime"] = workflow_runtime
        holder["launcher"] = RuntimeWorkflowLauncher(
            cast("DevelopmentRuntime", workflow_runtime),
            chat_service,
            conversations=conversations,
        )

    return ServingStack(
        chat_service=chat_service,
        broker=broker,
        conversations=conversations,
        workflow_runtime=workflow_runtime,
        harness_tool_provider=harness_tools,
        operator_policies=resolved_policies,
    )


# Re-exported for the local script, which keeps its own logging control
# service subclass and harness fallback.
__all__ = [
    "DEFAULT_SERVING_MODEL",
    "ServingStack",
    "build_serving_stack",
    "build_serving_workflow_runtime",
    "open_serving_harness_tools",
    "open_serving_model_provider",
]
