"""The workflow-launch control tool.

Starting a workflow is a model tool call, not a chat-message protocol.  The
model supplies the workflow id and the research idea; the runtime resolves the
workspace, chat, actor, trigger message, and caller anchor from trusted session
context, resolves the requested workflow id through the engine registry, and
launches it through the registered execute entry point.  The workflow, never
the model, owns phase transitions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.datum import DatumType, datum_from_result
from thesis.agent.runtime.session import RegisteredTool, ToolExecution
from thesis.workflows.core.registry import UnknownWorkflowError, WorkflowRegistry
from thesis.workflows.core.spec import WorkflowContext

#: The generic workflow-launch tool.  ``workflow`` selects the workflow.
TOOL_START_WORKFLOW = "start_workflow"

#: The hypothesis-development workflow id accepted by the launcher.
HYPOTHESIS_DEVELOPMENT_WORKFLOW = "hypothesis-development"

WORKFLOW_TOOL_NAMES: tuple[str, ...] = (TOOL_START_WORKFLOW,)


def _start_workflow_schema(workflow_ids: tuple[str, ...]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["workflow", "idea"],
        "properties": {
            "workflow": {
                "type": "string",
                "enum": list(workflow_ids),
                "description": "The workflow to start.",
            },
            "idea": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The research idea, hypothesis, or edge to develop and falsify."
                ),
            },
            "investigation_id": {
                "type": "string",
                "minLength": 1,
                "description": "Optional investigation to link the execution to.",
            },
        },
    }


class WorkflowLauncher(Protocol):
    """Launches one deterministic workflow from trusted context."""

    @property
    def workflows(self) -> WorkflowRegistry:
        """Return the registry that resolves a workflow id to its spec."""


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


def _failure(message: str) -> ToolExecution:
    return ToolExecution(
        ok=False,
        failure={"kind": "tool", "message": message, "tool": TOOL_START_WORKFLOW},
    )


def workflow_registered_tools(
    launcher: WorkflowLauncher,
    context: ToolContext,
) -> tuple[RegisteredTool, ...]:
    """Return the workflow-launch tools bound to ``launcher`` and ``context``."""

    def handle_start_workflow(arguments: Mapping[str, object]) -> ToolExecution:
        workflow = arguments.get("workflow")
        if not isinstance(workflow, str) or not workflow.strip():
            return _failure(f"unknown workflow: {workflow!r}")
        try:
            registration = launcher.workflows.resolve(workflow.strip())
        except UnknownWorkflowError as error:
            return _failure(str(error))
        idea = arguments.get("idea")
        if not isinstance(idea, str) or not idea.strip():
            return _failure("idea must be non-empty text")
        investigation_id = arguments.get("investigation_id")
        if investigation_id is not None and (
            not isinstance(investigation_id, str) or not investigation_id.strip()
        ):
            return _failure("investigation_id must be non-empty text when set")
        # The caller's anchor decides where the execution belongs: the root
        # chat for the top-level agent, the caller's node for a sub-agent.  The
        # trigger is the operator message that requested the launch, so the
        # launch never writes a second, duplicate trigger message.
        execution_context = WorkflowContext(
            workspace_id=context.workspace_id,
            chat_id=context.chat_id,
            actor_id=context.actor_id,
            trigger_message_id=context.trigger_message_id,
            intent_epoch=context.intent_epoch,
            anchor_sub_agent_id=context.sub_agent_id,
        )
        result = registration.execute(
            execution_context,
            ideas=idea.strip(),
            investigation_id=(
                None if investigation_id is None else investigation_id.strip()
            ),
        )
        structured = dict(result)
        return ToolExecution(
            ok=True,
            structured=structured,
            text=f"Workflow '{workflow}' started.",
            datums=(
                datum_from_result(
                    DatumType.WORKFLOW_EXECUTION.value,
                    ok=True,
                    structured=structured,
                    text=f"Workflow '{workflow}' started.",
                ),
            ),
        )

    spec = _function_tool(
        TOOL_START_WORKFLOW,
        "Start a deterministic research workflow. Starting a workflow is only "
        "possible through this tool. The runtime supplies the workspace and "
        "chat; you supply the workflow id and the idea. Launching hands the "
        "work to the workflow: it runs detached, routes its own phases and "
        "data, and reports back as its own configuration dictates. After it "
        "returns, reply with "
        "one short sentence saying you started it; do not perform its work, do "
        "not gather its inputs, and do not relay its requests.",
        _start_workflow_schema(launcher.workflows.ids()),
    )
    return (RegisteredTool(spec=spec, handler=handle_start_workflow),)


__all__ = [
    "HYPOTHESIS_DEVELOPMENT_WORKFLOW",
    "TOOL_START_WORKFLOW",
    "WORKFLOW_TOOL_NAMES",
    "WorkflowLauncher",
    "workflow_registered_tools",
]
