"""Compose one merged tool catalog for an agent session.

The catalog is the union of the quant-harness MCP tools, the Thesis control
tools (spawn, await, operator input), and the workflow-launch tools.  Every
handler is bound at composition time to the trusted
:class:`~thesis_runtime.contracts.context.ToolContext`, and the resulting registry
enforces the context's effective allowed-tool policy by default-deny.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import cast
from uuid import uuid4

from thesis.agent.harness_tools import HarnessToolProvider, HarnessToolResult
from thesis.experiments.mandate import DataWindowPolicy
from thesis.extensions.tools.exploration import (
    EXPLORATION_TOOL_NAMES,
    exploration_registered_tools,
)
from thesis.extensions.tools.knowledge import (
    KNOWLEDGE_TOOL_NAMES,
    knowledge_registered_tools,
)
from thesis.extensions.tools.program import (
    PROGRAM_TOOL_NAMES,
    ContextJobRetention,
    program_registered_tools,
)
from thesis.extensions.tools.sketch import SKETCH_TOOL_NAMES, sketch_registered_tools
from thesis.extensions.tools.strategy import (
    StrategyAuthor,
    StrategyAuthoringService,
    strategy_registered_tools,
)
from thesis.knowledge.service import KnowledgeScope, KnowledgeService
from thesis.persistence.submissions import SubmissionStore, retain_submission
from thesis.workspaces.programs import WorkspacePrograms
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.subagents.control_tools import (
    CONTROL_CHAT_TOOL_NAMES,
    CONTROL_NODE_TOOL_NAMES,
    ControlToolRuntime,
    SpawnObserver,
    control_registered_tools,
)
from thesis_runtime.subagents.policy import SpawnPolicy
from thesis_runtime.tools.registry import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis_runtime.tools.workflows import (
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
            *KNOWLEDGE_TOOL_NAMES,
            *PROGRAM_TOOL_NAMES,
            *SKETCH_TOOL_NAMES,
            *EXPLORATION_TOOL_NAMES,
        }
    )
)


def harness_session_name(name: str) -> str:
    """Native harness operations never share identities with workspace operations."""
    return f"harness_{name}" if name in PROGRAM_TOOL_NAMES else name


def _function_tool(
    name: str, description: str, parameters: Mapping[str, object]
) -> dict[str, object]:
    return {
        "name": name,
        "description": description,
        "parameters": dict(parameters),
    }


def _harness_handler(
    provider: HarnessToolProvider,
    name: str,
    *,
    context: ToolContext | None = None,
    submissions: SubmissionStore | None = None,
) -> Callable[[Mapping[str, object], ToolContext], ToolExecution]:
    def handle(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        submitted = dict(arguments)
        job_id: str | None = None
        if name in {"run_backtest", "run_study", "run_screener", "launch_trader"}:
            config = submitted.get("config")
            if (
                context is None
                or not isinstance(config, Mapping)
                or "config_path" in submitted
            ):
                return ToolExecution(
                    ok=False,
                    failure={
                        "kind": "invalid_arguments",
                        "message": "execution requires frozen inline config and trusted context",
                        "certainty": "not_started",
                        "retry_safe": True,
                    },
                )
            job_id = f"job-{uuid4().hex}"
            frozen = dict(config)
            frozen["job_id"] = job_id
            submitted["config"] = frozen
            submitted["job_id"] = job_id
            try:
                retain_submission(
                    submissions,
                    context.workspace_id,
                    job_id,
                    name,
                    {
                        "arguments": submitted,
                        "chat_id": context.chat_id,
                        "conversation_id": context.conversation_id,
                    },
                )
            except Exception as error:
                return ToolExecution(
                    ok=False,
                    structured={"job_id": job_id},
                    failure={
                        "kind": "persistence",
                        "message": str(error),
                        "certainty": "not_started",
                        "retry_safe": True,
                    },
                )
        try:
            result: HarnessToolResult = provider.call_tool(name, submitted)
        except Exception as error:
            if job_id is None:
                raise
            return ToolExecution(
                ok=False,
                structured={"job_id": job_id},
                failure={
                    "kind": "transport",
                    "message": str(error),
                    "certainty": "unknown_outcome",
                    "retry_safe": False,
                    "job_id": job_id,
                },
            )
        if job_id is not None and result.structured.get("job_id") != job_id:
            return ToolExecution(
                ok=False,
                structured={"job_id": job_id},
                failure={
                    "kind": "protocol",
                    "message": "harness acknowledgement did not match retained job_id",
                    "certainty": "unknown_outcome",
                    "retry_safe": False,
                    "job_id": job_id,
                },
            )

        structured = dict(result.structured)
        if name in ("get_job_status", "get_job_result"):
            accepted = structured.pop("accepted_input", None)
            if isinstance(accepted, Mapping):
                structured["accepted_input_reference"] = {
                    "job_id": structured.get("job_id"),
                    "input_digest": structured.get("input_digest"),
                    "programs": [
                        {
                            "program_id": item.get("instance_id"),
                            "bundle_hash": item.get("bundle_hash"),
                        }
                        for item in accepted.get("strategy_instances", [])
                        if isinstance(item, Mapping)
                    ],
                }
        if job_id is not None:
            structured["job_id"] = job_id
        if result.ok:
            return ToolExecution(
                ok=True,
                structured=structured,
                text=""
                if name in ("get_job_status", "get_job_result")
                else result.text,
            )
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
    *,
    context: ToolContext | None = None,
    submissions: SubmissionStore | None = None,
) -> tuple[RegisteredTool, ...]:
    """Return the discovered harness MCP tools as session tools."""
    tools: list[RegisteredTool] = []
    for spec in provider.list_tools():
        if spec.name == "launch_trader":
            # This optional native capability only accepts host-local paths;
            # it cannot yet satisfy immutable, cross-host submission retention.
            continue
        parameters = dict(spec.input_schema)
        if spec.name in {"run_backtest", "run_study", "run_screener"}:
            properties = dict(
                cast(Mapping[str, object], parameters.get("properties", {}))
            )
            properties.pop("config_path", None)
            properties.pop("job_id", None)
            properties["config"] = {"type": "object", "minProperties": 1}
            parameters = {
                "type": "object",
                "properties": properties,
                "required": ["config"],
                "additionalProperties": False,
            }
        tools.append(
            RegisteredTool(
                descriptor=_function_tool(
                    harness_session_name(spec.name), spec.description, parameters
                ),
                handler=_harness_handler(
                    provider, spec.name, context=context, submissions=submissions
                ),
                operation="read" if spec.read_only else "execute",
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
    data_window: DataWindowPolicy | None = None,
    job_retention: ContextJobRetention | None = None,
    submissions: SubmissionStore | None = None,
    workflow_tools: tuple[RegisteredTool, ...] = (),
) -> SessionToolRegistry:
    """Compose the merged, policy-scoped catalog for one session context.

    ``phase_completion_schema`` is the phase's declared completion JSON
    Schema; when the context belongs to a workflow phase agent, the catalog
    advertises it on ``request_phase_completion`` so the provider constrains
    the call at decode time.  The workflow-launch tool is advertised to
    contexts outside a workflow execution. A workflow may explicitly inject a
    narrowly registered child launcher through ``workflow_tools`` for a permitted
    phase; this does not expose the general launcher to every executor/helper.

    ``strategy_authoring`` is an explicit capability injection. Composition
    supplies it only for the authorized durable executor, never merely because
    a helper inherited the tool name in its policy.
    """
    tools: list[RegisteredTool] = list(workflow_tools)
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
        tools.extend(sketch_registered_tools(programs.sketches, context))
        tools.extend(exploration_registered_tools(programs))
        tools.extend(
            program_registered_tools(
                programs,
                context,
                data_window=data_window,
                job_retention=job_retention,
            )
        )
    if strategy_authoring is not None and programs is None:
        tools.extend(strategy_registered_tools(strategy_authoring, context))
    if harness_tools is not None:
        tools.extend(
            tool
            for tool in harness_registered_tools(
                harness_tools,
                context=context,
                submissions=submissions
                if submissions is not None
                else (None if programs is None else programs.submissions),
            )
            if programs is None
            or tool.name
            not in {harness_session_name(name) for name in PROGRAM_TOOL_NAMES}
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
        # execution does not receive the general launcher, so model creation cannot re-launch its
        # own workflow after reading the operator's "start the workflow" seed
        # text. Explicit phase-owned launchers arrive through workflow_tools.
        # A chat-spawned sub-agent outside a workflow keeps the general launcher.
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
