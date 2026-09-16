"""Durable asynchronous agent runtime for Thesis sub-agents and phases.

This package owns the chat-attached sub-agent model that agent-backed workflow
phases are built on: immutable specifications and budgets, durable sub-agent and
invocation stores, deterministic lifecycle guards, the Thesis control tools
(including the phase-completion handshake), and asynchronous result delivery to
parents.  The workflow state machine stays deterministic and owns every phase
transition; this package owns how an agent runs, calls tools, and requests
completion.
"""

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
from thesis.agent.runtime.lifecycle import (
    SubAgentLifecycle,
    SubAgentLifecycleError,
)
from thesis.agent.runtime.notify import SubAgentNotifier, TurnScheduler
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

__all__ = [
    "CONTROL_TOOL_NAMES",
    "CONTROL_TOOL_SPECS",
    "AwaitResult",
    "CompletionOutcome",
    "CompletionStatus",
    "ControlToolService",
    "ControlToolSpec",
    "DEFAULT_CONSTRUCTION_ATTEMPTS",
    "DEFAULT_MAX_CHILDREN",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_REPAIR_TURNS",
    "InMemorySubAgentStore",
    "OperatorInputRequest",
    "PersistenceSubAgentStore",
    "PhaseCompletionProtocol",
    "SpawnResult",
    "SubAgentBudget",
    "SubAgentLifecycle",
    "SubAgentLifecycleError",
    "SubAgentNotifier",
    "SubAgentSpec",
    "SubAgentStore",
    "TOOL_AWAIT_SUB_AGENT",
    "TOOL_REQUEST_OPERATOR_INPUT",
    "TOOL_REQUEST_PHASE_COMPLETION",
    "TOOL_REQUEST_SUB_AGENT_COMPLETION",
    "TOOL_SPAWN_SUB_AGENT",
    "TurnScheduler",
    "set_result_ref",
]
