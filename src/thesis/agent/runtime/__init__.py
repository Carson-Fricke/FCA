"""Durable asynchronous agent runtime for Thesis sub-agents and phases.

This package owns the chat-attached sub-agent model that agent-backed workflow
phases are built on: immutable specifications and budgets, durable sub-agent and
invocation stores, deterministic lifecycle guards, the Thesis control tools
(including the phase-completion handshake), and asynchronous result delivery to
parents.  The workflow state machine stays deterministic and owns every phase
transition; this package owns how an agent runs, calls tools, and requests
completion.
"""

from thesis.agent.runtime.child_runner import ChildRunner, RegistryFactory
from thesis.agent.runtime.context import ToolContext, ToolContextError
from thesis.agent.runtime.control import (
    CONTROL_TOOL_NAMES,
    CONTROL_TOOL_SPECS,
    TOOL_AWAIT_SUB_AGENT,
    TOOL_REQUEST_OPERATOR_INPUT,
    TOOL_REQUEST_PHASE_COMPLETION,
    TOOL_REQUEST_SUB_AGENT_COMPLETION,
    TOOL_SPAWN_SUB_AGENT,
    AwaitResult,
    CompletionOutcome,
    CompletionStatus,
    ControlToolService,
    ControlToolSpec,
    OperatorInputRequest,
    PhaseCompletionProtocol,
    SpawnResult,
    set_result_ref,
)
from thesis.agent.runtime.control_tools import (
    CONTROL_CHAT_TOOL_NAMES,
    ControlToolRuntime,
    SpawnObserver,
    control_registered_tools,
)
from thesis.agent.runtime.datum import (
    Datum,
    DatumType,
    datum_from_result,
    normalize_datums,
)
from thesis.agent.runtime.lifecycle import (
    SubAgentLifecycle,
    SubAgentLifecycleError,
)
from thesis.agent.runtime.notify import SubAgentNotifier, TurnScheduler
from thesis.agent.runtime.policy import (
    DEFAULT_ROOT_BUDGET_CEILING,
    RolePolicySpec,
    SpawnAuthorization,
    SpawnDenial,
    SpawnPolicy,
    SpawnPolicyError,
)
from thesis.agent.runtime.registry import (
    THESIS_TOOL_NAMES,
    build_session_registry,
    harness_registered_tools,
)
from thesis.agent.runtime.session import (
    DEFAULT_MAX_TOOL_ITERATIONS,
    AgentSession,
    CompletionProvider,
    RegisteredTool,
    SessionResult,
    SessionToolRegistry,
    SessionTurn,
    ToolExecution,
    ToolHandler,
)
from thesis.agent.runtime.specs import (
    DEFAULT_CONSTRUCTION_ATTEMPTS,
    DEFAULT_MAX_CHILDREN,
    DEFAULT_MAX_DEPTH,
    DEFAULT_REPAIR_TURNS,
    SubAgentBudget,
    SubAgentSpec,
)
from thesis.agent.runtime.store import (
    InMemorySubAgentStore,
    PersistenceSubAgentStore,
    SubAgentStore,
)
from thesis.agent.runtime.tree import (
    ChatSubAgentNode,
    ChatSubAgentProjectionError,
    ChatSubAgentTree,
    project_chat_sub_agents,
)
from thesis.agent.runtime.workflow_tools import (
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    TOOL_START_WORKFLOW,
    WORKFLOW_TOOL_NAMES,
    WorkflowLauncher,
    workflow_registered_tools,
)

__all__ = [
    "CONTROL_CHAT_TOOL_NAMES",
    "CONTROL_TOOL_NAMES",
    "CONTROL_TOOL_SPECS",
    "DEFAULT_MAX_TOOL_ITERATIONS",
    "HYPOTHESIS_DEVELOPMENT_WORKFLOW",
    "AgentSession",
    "AwaitResult",
    "ChildRunner",
    "ChatSubAgentNode",
    "ChatSubAgentProjectionError",
    "ChatSubAgentTree",
    "CompletionOutcome",
    "CompletionProvider",
    "CompletionStatus",
    "ControlToolRuntime",
    "ControlToolService",
    "ControlToolSpec",
    "Datum",
    "DatumType",
    "datum_from_result",
    "normalize_datums",
    "DEFAULT_CONSTRUCTION_ATTEMPTS",
    "DEFAULT_MAX_CHILDREN",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_REPAIR_TURNS",
    "DEFAULT_ROOT_BUDGET_CEILING",
    "InMemorySubAgentStore",
    "OperatorInputRequest",
    "PersistenceSubAgentStore",
    "PhaseCompletionProtocol",
    "RegisteredTool",
    "RegistryFactory",
    "RolePolicySpec",
    "SessionResult",
    "SessionToolRegistry",
    "SessionTurn",
    "SpawnAuthorization",
    "SpawnDenial",
    "SpawnObserver",
    "SpawnPolicy",
    "SpawnPolicyError",
    "SpawnResult",
    "SubAgentBudget",
    "SubAgentLifecycle",
    "SubAgentLifecycleError",
    "SubAgentNotifier",
    "SubAgentSpec",
    "SubAgentStore",
    "THESIS_TOOL_NAMES",
    "TOOL_AWAIT_SUB_AGENT",
    "TOOL_REQUEST_OPERATOR_INPUT",
    "TOOL_REQUEST_PHASE_COMPLETION",
    "TOOL_REQUEST_SUB_AGENT_COMPLETION",
    "TOOL_SPAWN_SUB_AGENT",
    "TOOL_START_WORKFLOW",
    "ToolContext",
    "ToolContextError",
    "ToolExecution",
    "ToolHandler",
    "TurnScheduler",
    "WORKFLOW_TOOL_NAMES",
    "WorkflowLauncher",
    "build_session_registry",
    "control_registered_tools",
    "harness_registered_tools",
    "project_chat_sub_agents",
    "set_result_ref",
    "workflow_registered_tools",
]
