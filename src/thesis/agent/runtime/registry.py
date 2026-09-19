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
from thesis.agent.runtime.policy import SpawnPolicy
from thesis.agent.runtime.session import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis.agent.runtime.workflow_tools import (
    WORKFLOW_TOOL_NAMES,
    WorkflowLauncher,
    workflow_registered_tools,
)

#: The static Thesis-owned tool names: control tools plus workflow launch.
#: The completion tools are policy-visible so a node can inherit them, while
#: the catalog only advertises them in a sub-agent/phase scope.
THESIS_TOOL_NAMES: tuple[str, ...] = tuple(
    sorted(
        {
            *CONTROL_CHAT_TOOL_NAMES,
            *CONTROL_NODE_TOOL_NAMES,
            *WORKFLOW_TOOL_NAMES,
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
) -> SessionToolRegistry:
    """Compose the merged, policy-scoped catalog for one session context."""
    tools: list[RegisteredTool] = []
    if harness_tools is not None:
        tools.extend(harness_registered_tools(harness_tools))
    if control is not None:
        tools.extend(
            control_registered_tools(
                control,
                context,
                on_spawn=on_spawn,
                policy=spawn_policy,
            )
        )
    if workflow is not None:
        tools.extend(workflow_registered_tools(workflow, context))
    policy: Sequence[str] | None = context.allowed_tools or None
    return SessionToolRegistry(tools, policy=policy)


__all__ = [
    "THESIS_TOOL_NAMES",
    "build_session_registry",
    "harness_registered_tools",
]
