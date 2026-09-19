"""Local Thesis API composition for the no-Azure development stack.

Binds the real chat surface (T211), the workspace DATA routes (T231), and
the screener router to a single uvicorn process on 127.0.0.1:8000, with a
LocalHarnessProcess backtest-launch boundary and CORS for the Svelte dev
server.  This is the composition `thesis serve` will eventually take over;
see .github/runbooks/thesis-local-stack.md.

Usage:
    uv run python scripts/local/thesis_local_api.py
Env:
    THESIS_API__BEARER_TOKEN (required)
    HARNESS_API_BEARER_TOKEN (optional, enables the remote status client)
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Mapping, Sequence

import uvicorn

if __name__ != "__main__":
    raise SystemExit("run as a script: uv run python scripts/local/thesis_local_api.py")

sys.path.insert(0, "src")

from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from thesis.agent.harness_tools import (  # noqa: E402
    HARNESS_TOOL_NAMES,
    HarnessMcpComposition,
    HarnessToolProvider,
    open_harness_mcp_composition,
    open_harness_tool_provider,
)
from thesis.agent.hypothesis_skills import (  # noqa: E402
    with_hypothesis_development_skills,
)
from thesis.agent.router import (  # noqa: E402
    ModelProfile,
    Role,
    RolePolicy,
)
from thesis.agent.runtime.child_runner import NodeTurnRunner  # noqa: E402
from thesis.agent.runtime.context import ToolContext  # noqa: E402
from thesis.agent.runtime.control import ControlToolService  # noqa: E402
from thesis.agent.runtime.delivery import (  # noqa: E402
    DeliveryService,
    NodeInputService,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle  # noqa: E402
from thesis.agent.runtime.policy import (  # noqa: E402
    OperatorPolicyRegistry,
    SpawnPolicy,
)
from thesis.agent.runtime.registry import (  # noqa: E402
    THESIS_TOOL_NAMES,
    build_session_registry,
)
from thesis.agent.runtime.store import PersistenceSubAgentStore  # noqa: E402
from thesis.agent.runtime.turns import NodeTurnCoordinator  # noqa: E402
from thesis.agent.session_reply import SessionReplyProvider  # noqa: E402
from thesis.agent.skills import SkillRegistry  # noqa: E402
from thesis.agent.tool_dispatch import with_harness_mcp  # noqa: E402
from thesis.chat.contracts import (  # noqa: E402
    WORKFLOW_NODE_ROLE,
    ChatMessage,
    MessageRole,
    RecordNotFound,
    SubAgentRecord,
    WorkflowExecutionState,
)
from thesis.chat.responder import ChatReplyService  # noqa: E402
from thesis.chat.streaming import InMemoryChatStreamBroker  # noqa: E402
from thesis.chat.workflow_launcher import RuntimeWorkflowLauncher  # noqa: E402
from thesis.cosmos.stack import open_cosmos_chat_stack  # noqa: E402
from thesis.http.application import (  # noqa: E402
    create_app,
    serving_workspace_store,
)
from thesis.http.workspaces import build_local_workspace_data  # noqa: E402
from thesis.integrations.harness import ProcessResult  # noqa: E402
from thesis.integrations.harness_activation import (  # noqa: E402
    HarnessBoundary,
    LocalHarnessProcess,
)
from thesis.integrations.mcp import (  # noqa: E402
    HarnessMcpError,
    HarnessMcpTopology,
    harness_mcp_topology_from_env,
)
from thesis.providers.openrouter import (  # noqa: E402
    OpenRouterProfile,
    OpenRouterProvider,
)
from thesis.providers.transports import (  # noqa: E402
    DEFAULT_SYSTEM_PROMPT,
    EnvironmentApiKeySource,
    HttpOpenRouterTransport,
    openrouter_reply_provider,
)
from thesis.research.hypothesis import WorkspaceHypothesisStore  # noqa: E402
from thesis.research.indicators import WorkspaceIndicatorStore  # noqa: E402
from thesis.research.models import WorkspaceResearchModelStore  # noqa: E402
from thesis.strategies.validation import (  # noqa: E402
    StrategyValidationRequest,
    StrategyValidationResult,
)
from thesis.workflows.local import McpBacktestRunner, McpStudyRunner  # noqa: E402
from thesis.workflows.registry.hypothesis_development import (  # noqa: E402
    HypothesisDevelopmentRequest,
)
from thesis.workflows.research_models import (  # noqa: E402
    BacktestResearchModelRunner,
)
from thesis.workflows.runtime import build_workflow_runtime  # noqa: E402
from thesis.workspaces.local import (  # noqa: E402
    LocalWorkspaceProvider,
    default_workspace_root,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("thesis.local_api")

WORKSPACE_ROOT = str(default_workspace_root())
HARNESS_BASE_URL = "http://127.0.0.1:8001"


def _real_subprocess_runner(argv):
    """Run a real local harness process via the uv-managed interpreter."""
    import subprocess

    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        shell=False,
    )
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


def _mcp_disabled() -> bool:
    return os.environ.get("THESIS_HARNESS_MCP", "1").strip().lower() in (
        "0",
        "false",
        "no",
    )


def _is_local_url(topology: HarnessMcpTopology) -> bool:
    """Return True when the MCP URL points at the loopback harness API."""
    url = topology.harness_url or ""
    return "127.0.0.1" in url or "localhost" in url


def _mcp_mode(topology: HarnessMcpTopology) -> str:
    """Return a short label for the selected MCP transport."""
    if _is_local_url(topology):
        return "local-http"
    if topology.harness_location == "remote":
        return "remote-http"
    return "local-stdio"


def _harness_composition() -> tuple[HarnessBoundary | None, HarnessToolProvider | None]:
    """Open exactly one harness MCP client and derive both consumers from it.

    Local and remote harnesses both use the harness HTTP ``/mcp`` endpoint: the
    local stack points at the local harness API (default
    ``http://127.0.0.1:8001``) and remote points at the deployed service, so
    there is one MCP mechanism and no Windows stdio subprocess path.  A single
    topology-aware client is initialized once with a bounded retry, and the
    backtest boundary and chat tool provider are derived from that same client.
    When MCP is disabled or stays unreachable after retries, the composition
    falls back to the direct local process boundary (loopback only) and a
    tool-free chat; the error is logged only after retries are exhausted.
    """
    os.environ.setdefault("HARNESS_MCP_URL", HARNESS_BASE_URL)
    topology = harness_mcp_topology_from_env()
    if _mcp_disabled():
        return LocalHarnessProcess(_real_subprocess_runner), None
    try:
        composition: HarnessMcpComposition = open_harness_mcp_composition(topology)
    except (HarnessMcpError, OSError) as error:
        print(
            f"harness MCP tool catalog unavailable after retries; chat replies "
            f"without tools: {error}",
            file=sys.stderr,
        )
        if _is_local_url(topology) or topology.harness_location == "local":
            return LocalHarnessProcess(_real_subprocess_runner), None
        return None, None
    provider = open_harness_tool_provider(client=composition.client)
    print(
        f"harness MCP tool catalog ready; chat tool calling enabled "
        f"({len(composition.tool_names)} tools via {_mcp_mode(topology)})",
        file=sys.stderr,
    )
    return composition.boundary, provider


class _WorkflowModelProvider:
    """Adapt the OpenRouter chat client to the router's provider port."""

    def __init__(self, provider: OpenRouterProvider) -> None:
        self._provider = provider

    def invoke(
        self,
        *,
        profile,  # noqa: ANN001
        prompt,  # noqa: ANN001
        allowed_tools,
        response_format=None,  # noqa: ANN001
    ):
        del profile, allowed_tools
        from thesis.agent.router import ModelResponse
        from thesis.providers.invocations import ChatMessage as ProviderChatMessage

        completion = self._provider.complete(
            messages=[ProviderChatMessage(role="user", content=prompt.rendered)],
            response_format=response_format,
        )
        return ModelResponse(output=completion.content)


class _StructuralStrategyValidator:
    """Accept a structurally valid bundle for local composition."""

    def validate(self, request: StrategyValidationRequest) -> StrategyValidationResult:
        return StrategyValidationResult(
            True, (), validation_id=f"local-{request.bundle_sha256[:16]}"
        )


class _LoggingControlToolService(ControlToolService):
    """Control surface that logs operator-input and transition decisions."""

    def request_operator_input(self, workspace_id, sub_agent_id, *, message, reason=""):  # noqa: ANN001, ANN201
        request = super().request_operator_input(
            workspace_id, sub_agent_id, message=message, reason=reason
        )
        LOGGER.info(
            "operator.input.request node=%s reason=%s",
            sub_agent_id,
            reason or "-",
        )
        return request

    def complete_phase(self, workspace_id, sub_agent_id, *, completion):  # noqa: ANN001, ANN201
        LOGGER.info("transition.request kind=phase node=%s", sub_agent_id)
        try:
            record = super().complete_phase(
                workspace_id, sub_agent_id, completion=completion
            )
        except Exception as error:
            LOGGER.warning(
                "transition.reject kind=phase node=%s error=%s", sub_agent_id, error
            )
            raise
        LOGGER.info(
            "transition.accept kind=phase node=%s status=%s",
            sub_agent_id,
            record.status.value,
        )
        return record

    def complete_sub_agent(self, workspace_id, sub_agent_id, *, result):  # noqa: ANN001, ANN201
        LOGGER.info("transition.request kind=sub_agent node=%s", sub_agent_id)
        try:
            record = super().complete_sub_agent(
                workspace_id, sub_agent_id, result=result
            )
        except Exception as error:
            LOGGER.warning(
                "transition.reject kind=sub_agent node=%s error=%s", sub_agent_id, error
            )
            raise
        LOGGER.info(
            "transition.accept kind=sub_agent node=%s status=%s",
            sub_agent_id,
            record.status.value,
        )
        return record


def _execution_epoch(workspace_id: str, execution_id: str) -> int | None:
    try:
        return chat_service.get_execution(workspace_id, execution_id).intent_epoch
    except RecordNotFound:
        return None


def _development_runtime(
    model_provider: OpenRouterProvider,
    harness_tools: HarnessToolProvider,
    subagent_lifecycle: SubAgentLifecycle | None = None,
):
    """Compose the workflow runtime for the falsification-first pipeline."""
    skills = SkillRegistry()
    profiles = {
        "local-openrouter": ModelProfile(
            profile_id="local-openrouter",
            version="1.0.0",
            provider="openrouter",
            model=os.environ.get("THESIS_MODEL", "z-ai/glm-5.3-flash"),
        )
    }

    def policy(role: Role) -> RolePolicy:
        base = RolePolicy(
            role=role,
            version="1.0.0",
            model_profile="local-openrouter",
            allowed_tools=(),
        )
        return with_hypothesis_development_skills(with_harness_mcp(base))

    runner = McpBacktestRunner(harness_tools)
    # Research models always compile to a ``study`` shell, so the falsification
    # phase must execute through ``run_study``; the backtest runner would reject
    # the study configuration before any computation.  The durable workspace
    # stores are bound to the runner itself: model publications, fits, and run
    # records persist under the workspace models/ tree instead of an
    # in-process store that dies with the API process.
    workspaces = LocalWorkspaceProvider(WORKSPACE_ROOT)
    research_runner = BacktestResearchModelRunner(
        McpStudyRunner(harness_tools),
        models=WorkspaceResearchModelStore(workspaces),
        indicators=WorkspaceIndicatorStore(workspaces),
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
        hypothesis_store=WorkspaceHypothesisStore(WORKSPACE_ROOT),
        subagent_lifecycle=subagent_lifecycle,
        turn_scheduler=_schedule_chat_turn,
    )


_harness, _harness_tools = _harness_composition()

# Conversations persist in Cosmos DB (the local emulator by default) and every
# message/event shares one per-chat timeline served by the unified stream.
chat_service, timeline = open_cosmos_chat_stack()
chat_stream_broker = InMemoryChatStreamBroker()

workspace_data = build_local_workspace_data(
    workspace_root=WORKSPACE_ROOT,
    harness_boundary=_harness,
    harness_base_url=HARNESS_BASE_URL,
    chat_service=chat_service,
    workspace_store=serving_workspace_store(),
)

#: Every tool the top-level chat agent may call: the full harness catalog plus
#: every Thesis-owned control tool (spawn, await, operator input, and workflow
#: launch).  A workflow's phase sub-agents are a different mechanism entirely:
#: the workflow state machine spawns them deterministically through the same
#: durable command contract, without the top-level agent calling any tool.
_AGENT_ALLOWED_TOOLS: tuple[str, ...] = tuple(
    sorted({*HARNESS_TOOL_NAMES, *THESIS_TOOL_NAMES})
)


def _trigger_message_id(history: Sequence[ChatMessage]) -> str | None:
    """Return the operator message that requested the current agent turn."""
    for message in reversed(history):
        if message.role is MessageRole.USER:
            return message.message_id
    return None


def _log_node_trigger(workspace_id: str, chat_id: str, sub_agent_id: str) -> None:
    """Log the operator answer that drives a node turn, when one is present."""
    for message in reversed(chat_service.list_messages(workspace_id, chat_id)):
        data = message.data or {}
        if str(data.get("subAgentId")) != sub_agent_id:
            continue
        if message.role is MessageRole.USER:
            if data.get("kind") == "operator_input":
                LOGGER.info(
                    "operator.input.answer node=%s actor=%s",
                    sub_agent_id,
                    message.actor_id,
                )
            return


def _run_root_turn(service, workspace_id: str, chat_id: str) -> None:  # noqa: ANN001
    LOGGER.info("turn.start anchor=root")
    started = time.monotonic()
    try:
        service.respond(workspace_id, chat_id)
    finally:
        LOGGER.info(
            "turn.end anchor=root duration_ms=%d",
            int((time.monotonic() - started) * 1000),
        )


def _schedule_chat_turn(workspace_id: str, chat_id: str, sub_agent_id: str) -> None:
    """Run one scheduled turn at the root chat or at one node anchor.

    The empty sub-agent id is the root anchor: the top-level reply service
    answers.  An anchor owned by a workflow execution re-enters the deterministic
    workflow through the runtime, so an operator answer at a phase node or a
    completion delivered to the execution's workflow node drives the next phase.
    Every other node anchor runs the T333 node-turn runner over that node's own
    anchored transcript, so a node delivery never leaks into the root prompt.
    """
    if sub_agent_id:
        record = (
            None
            if subagent_store is None
            else subagent_store.get_sub_agent(workspace_id, sub_agent_id)
        )
        if record is not None and record.role == WORKFLOW_NODE_ROLE:
            LOGGER.info("delivery.skip workflow node anchor=%s", sub_agent_id)
            return
        coordinator = node_turn_coordinator
        if coordinator is None:
            return
        LOGGER.info("delivery.schedule anchor=%s", sub_agent_id)
        _log_node_trigger(workspace_id, chat_id, sub_agent_id)
        coordinator.schedule(workspace_id, chat_id, sub_agent_id)
        return
    service = reply_service
    if service is None:
        return
    LOGGER.info("delivery.schedule anchor=root")
    threading.Thread(
        target=_run_root_turn,
        args=(service, workspace_id, chat_id),
        daemon=True,
    ).start()


# Wire a live model provider only when a key is present; otherwise chat stays
# a durable message surface with no reply, rather than failing every send.
reply_service = None
node_turn_runner = None
node_turn_coordinator = None
workflow_runtime = None
harness_tools = None
subagent_store: PersistenceSubAgentStore | None = None
_TERMINAL_EXECUTION_STATES = frozenset(
    {
        WorkflowExecutionState.COMPLETED,
        WorkflowExecutionState.FAILED,
        WorkflowExecutionState.STOPPED,
        WorkflowExecutionState.CANCELLED,
    }
)
if os.environ.get("OPENROUTER_API_KEY"):
    model_name = os.environ.get("THESIS_MODEL", "z-ai/glm-5.3-flash")
    provider_profile = OpenRouterProfile(
        profile_id="local-openrouter",
        model=model_name,
    )
    model_provider = OpenRouterProvider(
        provider_profile,
        EnvironmentApiKeySource(),
        HttpOpenRouterTransport(provider_profile.base_url),
    )
    harness_tools = _harness_tools
    subagent_store = PersistenceSubAgentStore(chat_service.persistence)
    # Permissive by default: undeclared roles are allowed, children inherit the
    # caller's tools, and root spawning is authorized. Only escalation (a child
    # gaining a tool its caller lacks) and the root budget ceiling are enforced.
    spawn_policy = SpawnPolicy()
    lifecycle = SubAgentLifecycle(
        chat_service,
        store=subagent_store,
        max_depth=spawn_policy.max_depth,
        max_children=spawn_policy.max_children,
    )

    def _apply_phase_completion(
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        """Apply one phase agent's completion through the workflow object."""
        if workflow_runtime is None or record.workflow_execution_id is None:
            return None
        execution = chat_service.get_execution(
            workspace_id, record.workflow_execution_id
        )
        request = HypothesisDevelopmentRequest.from_execution(chat_service, execution)
        workflow = workflow_runtime.build_hypothesis_development(chat=chat_service)
        return workflow.apply_phase_completion(request, record.sub_agent_id, completion)

    control_service = _LoggingControlToolService(
        lifecycle=lifecycle,
        store=subagent_store,
        inputs=NodeInputService(chat=chat_service, store=subagent_store),
        delivery=DeliveryService(chat_service, schedule=_schedule_chat_turn),
        execution_epochs=_execution_epoch,
        phase_completions=_apply_phase_completion,
    )
    if harness_tools is not None:
        workflow_runtime = _development_runtime(
            model_provider, harness_tools, lifecycle
        )
    launcher = (
        None
        if workflow_runtime is None
        else RuntimeWorkflowLauncher(
            workflow_runtime,
            chat_service,
            turn_scheduler=_schedule_chat_turn,
        )
    )

    def _registry_for(context: ToolContext):
        return build_session_registry(
            context=context,
            harness_tools=harness_tools,
            control=control_service,
            workflow=launcher,
            spawn_policy=spawn_policy,
        )

    def _fail_phase_execution(record: SubAgentRecord, error: Exception) -> None:
        """Fail the owning workflow execution after a phase-agent fault."""
        if workflow_runtime is None or record.workflow_execution_id is None:
            return
        execution = chat_service.get_execution(
            record.workspace_id, record.workflow_execution_id
        )
        request = HypothesisDevelopmentRequest.from_execution(chat_service, execution)
        workflow = workflow_runtime.build_hypothesis_development(chat=chat_service)
        workflow.fail_execution(request, reason=str(error) or type(error).__name__)

    node_turn_runner = NodeTurnRunner(
        chat=chat_service,
        store=subagent_store,
        provider=model_provider,
        registry_factory=_registry_for,
        lifecycle=lifecycle,
        on_fault=_fail_phase_execution,
        delivery=DeliveryService(chat_service, schedule=_schedule_chat_turn),
    )
    node_turn_coordinator = NodeTurnCoordinator(
        runner=node_turn_runner,
        chat=chat_service,
        store=subagent_store,
    )

    def _provider_for(workspace_id: str, chat_id: str, history: Sequence[ChatMessage]):
        context = ToolContext(
            workspace_id=workspace_id,
            chat_id=chat_id,
            conversation_id=chat_id,
            actor_id="thesis-agent",
            trigger_message_id=_trigger_message_id(history),
            allowed_tools=_AGENT_ALLOWED_TOOLS,
        )
        return SessionReplyProvider(
            workspace_id=workspace_id,
            chat_id=chat_id,
            conversation_id=chat_id,
            provider=model_provider,
            registry=_registry_for(context),
            store=subagent_store,
        )

    reply_service = ChatReplyService(
        chat_service=chat_service,
        broker=chat_stream_broker,
        provider=openrouter_reply_provider(
            model_provider, system_prompt=DEFAULT_SYSTEM_PROMPT
        ),
        provider_factory=(_provider_for if harness_tools is not None else None),
        development_runtime=workflow_runtime,
    )

app = create_app(
    chat_service=chat_service,
    workspace_data=workspace_data,
    chat_stream_broker=chat_stream_broker,
    chat_reply_service=reply_service,
    workflow_runtime=workflow_runtime,
    harness_tool_provider=harness_tools,
    timeline=timeline,
    sub_agent_turn=_schedule_chat_turn if reply_service is not None else None,
)
# The composition point for per-node answering policy; the T333 default is
# human-only, and a future autonomous node changes this registry, not the route.
app.state.operator_policies = OperatorPolicyRegistry()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
