"""Compose one merged tool catalog for an agent session.

The catalog is the union of the quant-harness MCP tools, the Thesis control
tools (spawn, await, operator input), and the workflow-launch tools.  Every
handler is bound at composition time to the trusted
:class:`~thesis.agent.runtime.context.ToolContext`, and the resulting registry
enforces the context's effective allowed-tool policy by default-deny.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from thesis.agent.harness_tools import HarnessToolProvider, HarnessToolResult
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.control_tools import (
    CONTROL_CHAT_TOOL_NAMES,
    CONTROL_NODE_TOOL_NAMES,
    ControlToolRuntime,
    SpawnObserver,
    control_registered_tools,
)
from thesis.agent.runtime.knowledge_tools import (
    KNOWLEDGE_TOOL_NAMES,
    knowledge_registered_tools,
)
from thesis.agent.runtime.policy import SpawnPolicy
from thesis.agent.runtime.program_tools import (
    PROGRAM_TOOL_NAMES,
    program_registered_tools,
)
from thesis.agent.runtime.session import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis.agent.runtime.strategy_tools import (
    StrategyAuthor,
    StrategyAuthoringService,
    strategy_registered_tools,
)
from thesis.agent.runtime.workflow_tools import (
    WORKFLOW_TOOL_NAMES,
    WorkflowLauncher,
    workflow_registered_tools,
)
from thesis.knowledge.service import KnowledgeScope, KnowledgeService
from thesis.workspaces.programs import WorkspacePrograms

#: The static Thesis-owned tool names: control tools plus workflow launch.
#: The completion tools are policy-visible so a node can inherit them, while
#: the catalog only advertises them in a sub-agent/phase scope.
THESIS_TOOL_NAMES: tuple[str, ...] = tuple(
    sorted(
        {
            *CONTROL_CHAT_TOOL_NAMES,
            *CONTROL_NODE_TOOL_NAMES,
            *WORKFLOW_TOOL_NAMES,
            *KNOWLEDGE_TOOL_NAMES,
            *PROGRAM_TOOL_NAMES,
        }
    )
)


def _function_tool(
    name: str, description: str, parameters: Mapping[str, object]
) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": dict(parameters),
        },
    }


def _harness_handler(
    provider: HarnessToolProvider, name: str
) -> Callable[[Mapping[str, object]], ToolExecution]:
    def handle(arguments: Mapping[str, object]) -> ToolExecution:
        result: HarnessToolResult = provider.call_tool(name, dict(arguments))
        structured = dict(result.structured)
        if result.ok:
            return ToolExecution(ok=True, structured=structured, text=result.text)
        failure: Mapping[str, object]
        if result.failure is None:
            failure = {"kind": "harness", "message": "harness tool call failed"}
        else:
            failure = dict(result.failure.as_dict())
        return ToolExecution(
            ok=False, structured=structured, text=result.text, failure=failure
        )

    return handle


def harness_registered_tools(
    provider: HarnessToolProvider,
) -> tuple[RegisteredTool, ...]:
    """Return the discovered harness MCP tools as session tools."""
    tools: list[RegisteredTool] = []
    for spec in provider.list_tools():
        tools.append(
            RegisteredTool(
                spec=_function_tool(spec.name, spec.description, spec.input_schema),
                handler=_harness_handler(provider, spec.name),
            )
        )
    return tuple(tools)


def build_session_registry(
    *,
    context: ToolContext,
    harness_tools: HarnessToolProvider | None = None,
    control: ControlToolRuntime | None = None,
    workflow: WorkflowLauncher | None = None,
    on_spawn: SpawnObserver | None = None,
    spawn_policy: SpawnPolicy | None = None,
    phase_completion_schema: Mapping[str, object] | None = None,
    strategy_authoring: StrategyAuthor | StrategyAuthoringService | None = None,
    knowledge: KnowledgeService | None = None,
    knowledge_scope: KnowledgeScope = "both",
    knowledge_collections: tuple[str, ...] | None = None,
    programs: WorkspacePrograms | None = None,
) -> SessionToolRegistry:
    """Compose the merged, policy-scoped catalog for one session context.

    ``phase_completion_schema`` is the phase's declared completion JSON
    Schema; when the context belongs to a workflow phase agent, the catalog
    advertises it on ``request_phase_completion`` so the provider constrains
    the call at decode time.  The workflow-launch tool is advertised to
    contexts outside a workflow execution: a phase agent or an agent spawned
    under one cannot launch a workflow, while a chat-spawned sub-agent can.

    ``strategy_authoring`` is an explicit capability injection. Composition
    supplies it only for the authorized durable executor, never merely because
    a helper inherited the tool name in its policy.
    """
    tools: list[RegisteredTool] = []
    if knowledge is not None:
        tools.extend(
            knowledge_registered_tools(
                knowledge,
                context,
                knowledge_scope,
                allowed_collections=knowledge_collections,
            )
        )
    if programs is not None:
        tools.extend(program_registered_tools(programs, context))
    if strategy_authoring is not None and programs is None:
        tools.extend(strategy_registered_tools(strategy_authoring, context))
    if harness_tools is not None:
        tools.extend(
            tool
            for tool in harness_registered_tools(harness_tools)
            if programs is None or tool.name not in PROGRAM_TOOL_NAMES
        )
    if control is not None:
        tools.extend(
            control_registered_tools(
                control,
                context,
                on_spawn=on_spawn,
                policy=spawn_policy,
                phase_completion_schema=phase_completion_schema,
            )
        )
    if workflow is not None and context.workflow_execution_id is None:
        # Workflow launch is scoped at composition: an agent inside a workflow
        # execution (any phase agent, or a helper spawned under one) never
        # advertises it, so the hypothesis-creation agent cannot re-launch its
        # own workflow after reading the operator's "start the workflow" seed
        # text. A chat-spawned sub-agent outside a workflow keeps the launcher.
        tools.extend(workflow_registered_tools(workflow, context))
    # The catalog is exactly the configured policy intersected with the
    # registered tools: an explicit empty policy advertises nothing, and only
    # an unconfigured policy (``None``) leaves the full catalog in place.
    policy: Sequence[str] | None = context.allowed_tools
    return SessionToolRegistry(tools, policy=policy)


__all__ = [
    "THESIS_TOOL_NAMES",
    "build_session_registry",
    "harness_registered_tools",
]
