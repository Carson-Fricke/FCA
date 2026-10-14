"""Compose chat, conversations, and workflow services for local and deployed HTTP."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
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
from thesis.agent.router import ModelProfile, ModelResponse
from thesis.agent.skills import SkillRegistry
from thesis.application.development import DevelopmentRuntime
from thesis.application.prompts import FCA_SYSTEM_PROMPT
from thesis.application.workflow_launcher import (
    Detach,
    RuntimeWorkflowLauncher,
    daemon_thread,
)
from thesis.application.workflows import (
    WorkflowRuntime,
    bind_workflow_session_services,
    build_workflow_runtime,
)
from thesis.extensions.tools.registry import (
    THESIS_TOOL_NAMES,
    build_session_registry,
    harness_session_name,
)
from thesis.extensions.workflows.bindings import (
    WorkflowSessionBindings,
    WorkflowSessionServices,
)
from thesis.extensions.workflows.hypothesis_development.session_services import (
    role_policies,
)
from thesis.integrations.mcp import HarnessMcpError, harness_mcp_topology_from_env
from thesis.knowledge.embeddings import default_embedder
from thesis.knowledge.service import KnowledgeService, validate_scope
from thesis.persistence.submissions import SubmissionStore
from thesis.research.hypothesis import HypothesisStore, WorkspaceHypothesisStore
from thesis.research.investigations import Investigations
from thesis.research.models import ResearchModelStore, WorkspaceResearchModelStore
from thesis.workflows.backtests import BacktestRunRequest
from thesis.workflows.local import McpBacktestRunner, McpStudyRunner
from thesis.workflows.research_models import BacktestResearchModelRunner
from thesis.workspaces.local import LocalWorkspaceProvider, default_workspace_root
from thesis.workspaces.programs import WorkspacePrograms
from thesis.workspaces.repository import WorkspaceRepositoryProvider
from thesis_runtime.adapters.providers.families import model_family_adapter
from thesis_runtime.adapters.providers.openrouter import (
    OpenRouterProfile,
    OpenRouterProvider,
)
from thesis_runtime.adapters.providers.session_adapter import NativeSessionAdapter
from thesis_runtime.adapters.providers.transports import (
    EnvironmentApiKeySource,
    HttpOpenRouterTransport,
)
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.messages import MessageContent, SessionMessage
from thesis_runtime.contracts.records import RecordNotFound, SubAgentRecord
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.execution.worker import WorkStore
from thesis_runtime.subagents.control import ControlToolService
from thesis_runtime.subagents.lifecycle import SubAgentLifecycle
from thesis_runtime.subagents.policy import OperatorPolicyRegistry, SpawnPolicy
from thesis_runtime.subagents.store import PersistenceSubAgentStore
from thesis_runtime.tools.registry import SessionToolRegistry
from thesis_runtime.workflows.execution_store import ExecutionIdentity, ExecutionStore
from thesis_runtime.workflows.registry import WorkflowRegistry
from thesis_runtime.workflows.state import TERMINAL_EXECUTION_STATES

DEFAULT_SERVING_MODEL = "deepseek/deepseek-v4.1-flash"
_SERVING_PROFILE_ID = "serving-openrouter"


class _WorkflowModelProvider:
    """Adapt the configured chat client to the workflow router provider port."""

    def __init__(self, provider: OpenRouterProvider) -> None:
        self._session = NativeSessionAdapter(
            provider, family=model_family_adapter(provider.model)
        )

    def invoke(
        self,
        *,
        profile: ModelProfile,
        prompt: PromptContext,
        allowed_tools: tuple[str, ...],
        response_format: Mapping[str, object] | None = None,
    ) -> ModelResponse:
        del profile, allowed_tools
        completion = self._session.complete(
            messages=[
                SessionMessage(
                    "workflow-prompt",
                    "incoming",
                    MessageContent(prompt.rendered, "prompt"),
                )
            ],
            response_format=response_format,
        )
        return ModelResponse(output=completion.content.text)


@dataclass(frozen=True, slots=True)
class ServingStack:
    """The composed services consumed by the HTTP application."""

    chat_service: ChatService
    conversations: Conversations | None
    workflow_runtime: WorkflowRuntime | None
    harness_tool_provider: HarnessToolProvider | None
    operator_policies: OperatorPolicyRegistry
    workflow_registry: WorkflowRegistry | None = None


@dataclass(slots=True)
class _ServingComposition:
    """Late-bound services at the conversation/workflow composition boundary."""

    control: ControlToolService | None = None
    workflow: WorkflowSessionServices | None = None
    launcher: RuntimeWorkflowLauncher | None = None


def _workspace_stores(
    workspace_root: str | Path | None,
    workspace_repositories: WorkspaceRepositoryProvider | None = None,
) -> tuple[ResearchModelStore | None, HypothesisStore | None]:
    """Bind research stores to verified workspace paths when configured."""
    if workspace_repositories is not None:
        return (
            WorkspaceResearchModelStore(workspace_repositories),
            WorkspaceHypothesisStore(workspace_repositories),
        )
    if workspace_root is None:
        return None, None
    repositories = LocalWorkspaceProvider(str(workspace_root))
    return WorkspaceResearchModelStore(repositories), WorkspaceHypothesisStore(
        repositories
    )


def build_serving_workflow_runtime(
    model_provider: OpenRouterProvider,
    harness_tools: HarnessToolProvider,
    *,
    conversations: Conversations | None = None,
    execution_store: ExecutionStore | None = None,
    workspace_root: str | Path | None = None,
    detach: Detach | None = daemon_thread,
    work_store: WorkStore | None = None,
    workspace_repositories: WorkspaceRepositoryProvider | None = None,
    programs: WorkspacePrograms | None = None,
    submissions: SubmissionStore | None = None,
) -> WorkflowRuntime:
    """Bind provider, workspace, and cancellation ports for workflow execution."""
    models, hypothesis_store = _workspace_stores(workspace_root, workspace_repositories)

    def cancelled(request: BacktestRunRequest) -> bool:
        if conversations is None:
            return False
        try:
            execution = conversations.chat.get_execution(
                request.workspace_id, request.workflow_execution_id
            )
        except RecordNotFound:
            return True
        return execution.state in TERMINAL_EXECUTION_STATES

    return build_workflow_runtime(
        role_policies=role_policies(_SERVING_PROFILE_ID),
        model_profiles={
            _SERVING_PROFILE_ID: ModelProfile(
                profile_id=_SERVING_PROFILE_ID,
                version="1.0.0",
                provider="openrouter",
                model=_serving_model_name(),
            )
        },
        providers={"openrouter": _WorkflowModelProvider(model_provider)},
        skills=SkillRegistry(),
        runner=McpBacktestRunner(
            harness_tools, cancelled=cancelled, submissions=submissions
        ),
        research_runner=BacktestResearchModelRunner(
            McpStudyRunner(harness_tools, cancelled=cancelled, submissions=submissions),
            models=models,
        ),
        harness_tools=harness_tools,
        programs=programs,
        hypothesis_store=hypothesis_store,
        conversations=conversations,
        execution_store=execution_store,
        detach=detach,
        work_store=work_store,
        knowledge_scope=validate_scope(
            os.environ.get("THESIS_KNOWLEDGE_SCOPE", "both")
        ),
    )


def _serving_model_name() -> str:
    return (os.environ.get("THESIS_MODEL") or "").strip() or DEFAULT_SERVING_MODEL


def open_serving_model_provider() -> OpenRouterProvider | None:
    """Open the optional model provider; missing credentials leave durable chat only."""
    if not (os.environ.get("OPENROUTER_API_KEY") or "").strip():
        return None
    profile = OpenRouterProfile(
        profile_id=_SERVING_PROFILE_ID, model=_serving_model_name()
    )
    return OpenRouterProvider(
        profile, EnvironmentApiKeySource(), HttpOpenRouterTransport(profile.base_url)
    )


def open_serving_harness_tools() -> HarnessToolProvider | None:
    """Open the configured MCP catalog, leaving workflows unwired if unavailable."""
    if not (os.environ.get("HARNESS_MCP_URL") or "").strip():
        return None
    try:
        composition = open_harness_mcp_composition(harness_mcp_topology_from_env())
    except (HarnessMcpError, OSError) as error:
        print(
            f"harness MCP tool catalog unavailable; the workflow runtime is not composed: {error}",
            file=sys.stderr,
        )
        return None
    return open_harness_tool_provider(client=composition.client)


def build_serving_stack(
    *,
    chat_service: ChatService,
    investigations: Investigations | None = None,
    model_provider: OpenRouterProvider | None = None,
    harness_tools: HarnessToolProvider | None = None,
    execution_store: ExecutionStore | None = None,
    workspace_root: str | Path | None = None,
    submissions: SubmissionStore | None = None,
    detach: Detach | None = daemon_thread,
    work_store: WorkStore | None = None,
    policies: OperatorPolicyRegistry | None = None,
    control_factory: Callable[..., ControlToolService] = ControlToolService,
    on_turn_error: Callable[[tuple[str, str, Exception]], None] | None = None,
    workspace_repositories: WorkspaceRepositoryProvider | None = None,
    knowledge_service: KnowledgeService | None = None,
) -> ServingStack:
    """Compose one conversation runner and bind workflow-owned session services."""
    resolved_policies = policies if policies is not None else OperatorPolicyRegistry()
    if model_provider is None:
        return ServingStack(chat_service, None, None, None, resolved_policies)
    store = PersistenceSubAgentStore(chat_service.persistence)
    knowledge = knowledge_service or KnowledgeService(
        workspace_repositories
        or LocalWorkspaceProvider(
            workspace_root if workspace_root is not None else default_workspace_root()
        ),
        embedder=default_embedder(),
    )
    spawn_policy = SpawnPolicy()
    lifecycle = SubAgentLifecycle(
        chat_service,
        store=store,
        max_depth=spawn_policy.max_depth,
        max_children=spawn_policy.max_children,
    )
    resolved = _ServingComposition()
    programs = WorkspacePrograms(
        workspace_repositories
        or LocalWorkspaceProvider(
            workspace_root if workspace_root is not None else default_workspace_root()
        ),
        harness_tools,
        submissions=submissions,
    )
    allowed_tools = tuple(
        sorted(
            {
                *(harness_session_name(name) for name in HARNESS_TOOL_NAMES),
                *THESIS_TOOL_NAMES,
            }
        )
    )

    def execution_epoch(workspace_id: str, execution_id: str) -> int | None:
        try:
            return chat_service.get_execution(workspace_id, execution_id).intent_epoch
        except RecordNotFound:
            return None

    def registry_for(context: ToolContext) -> SessionToolRegistry:
        if resolved.control is None:
            raise RuntimeError("conversation control service is not bound")
        if context.workflow_execution_id is not None and resolved.workflow is None:
            raise RuntimeError("workflow session services are not bound")
        bindings = (
            WorkflowSessionBindings()
            if resolved.workflow is None
            else resolved.workflow.resolve(context)
        )
        return build_session_registry(
            context=context,
            harness_tools=harness_tools,
            control=resolved.control,
            workflow=resolved.launcher,
            spawn_policy=spawn_policy,
            phase_completion_schema=bindings.completion_schema,
            workflow_tools=bindings.tools,
            strategy_authoring=bindings.strategy_authoring,
            knowledge=knowledge,
            knowledge_scope=bindings.knowledge_scope,
            knowledge_collections=bindings.knowledge_collections,
            programs=programs
            if context.workflow_execution_id is None
            else cast(WorkspacePrograms | None, bindings.programs),
            data_window=bindings.data_window,
            job_retention=bindings.job_retention,
        )

    def apply_completion(
        workspace_id: str, record: SubAgentRecord, completion: Mapping[str, object]
    ) -> Mapping[str, object] | None:
        if resolved.workflow is None:
            return None
        return resolved.workflow.apply_completion(workspace_id, record, completion)

    def validate_completion(
        workspace_id: str, record: SubAgentRecord, completion: Mapping[str, object]
    ) -> str | None:
        if resolved.workflow is None:
            return None
        return resolved.workflow.validate_completion(workspace_id, record, completion)

    def fail_execution(record: SubAgentRecord, error: Exception) -> None:
        if resolved.workflow is not None:
            resolved.workflow.fail(record, error)

    conversations = Conversations(
        chat=chat_service,
        store=store,
        work_store=work_store,
        provider=NativeSessionAdapter(
            model_provider, family=model_family_adapter(model_provider.model)
        ),
        registry_factory=registry_for,
        root_allowed_tools=allowed_tools,
        root_system_prompt=FCA_SYSTEM_PROMPT,
        lifecycle=lifecycle,
        policies=resolved_policies,
        on_fault=fail_execution,
        on_turn_error=on_turn_error,
        read_consumed_input=chat_service.read_consumed_input,
        acknowledge_input=chat_service.acknowledge_input,
    )
    resolved.control = control_factory(
        conversations=conversations,
        execution_epochs=execution_epoch,
        phase_completions=apply_completion,
        contract_validator=validate_completion,
    )
    runtime: WorkflowRuntime | None = None
    if harness_tools is not None:
        runtime = build_serving_workflow_runtime(
            model_provider,
            harness_tools,
            conversations=conversations,
            execution_store=execution_store,
            workspace_root=workspace_root,
            workspace_repositories=workspace_repositories,
            detach=detach,
            work_store=work_store,
            programs=programs,
            submissions=submissions,
        )
    resolved.workflow = bind_workflow_session_services(
        runtime=runtime,
        chat=chat_service,
        store=store,
        activity=conversations.execute_runtime_tool,
        programs=programs,
        knowledge_scope=validate_scope(
            os.environ.get("THESIS_KNOWLEDGE_SCOPE", "both")
        ),
    )
    if runtime is not None:
        resolved.launcher = RuntimeWorkflowLauncher(
            cast("DevelopmentRuntime", runtime),
            chat_service,
            conversations=conversations,
            investigations=investigations,
            detach=(lambda task: task()) if work_store is not None else detach,
            research_registration=runtime.research_registration(chat=chat_service),
        )
    return ServingStack(
        chat_service,
        conversations,
        runtime,
        harness_tools,
        resolved_policies,
        resolved.launcher.workflows if resolved.launcher is not None else None,
    )


__all__ = [
    "DEFAULT_SERVING_MODEL",
    "ServingStack",
    "build_serving_stack",
    "build_serving_workflow_runtime",
    "open_serving_harness_tools",
    "open_serving_model_provider",
]


def build_serving_worker(
    stack: ServingStack,
    work_store: WorkStore,
    ownership,
    executions: ExecutionStore,
    recovery_records: Callable[[], tuple[ExecutionIdentity, ...]] | None = None,
):
    """Bind FCA registrations into the generic durable worker."""
    from thesis_runtime.hosting.worker import RuntimeHost, build_worker

    return build_worker(
        RuntimeHost(stack.chat_service, stack.conversations, stack.workflow_registry),
        work_store,
        ownership,
        executions,
        recovery_records,
    )
