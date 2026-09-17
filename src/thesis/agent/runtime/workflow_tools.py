"""The workflow-launch control tool.

Starting a workflow is a model tool call, not a chat-message protocol.  The
model supplies the research idea; the runtime resolves the workspace, chat, and
actor from trusted session context, launches the deterministic workflow through
the injected launcher, and returns the execution handle as a durable tool
result.  The workflow, never the model, owns phase transitions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.datum import DatumType, datum_from_result
from thesis.agent.runtime.session import RegisteredTool, ToolExecution

#: The generic workflow-launch tool.  ``workflow`` selects the workflow.
TOOL_START_WORKFLOW = "start_workflow"

#: The hypothesis-development workflow id accepted by the launcher.
HYPOTHESIS_DEVELOPMENT_WORKFLOW = "hypothesis-development"

WORKFLOW_TOOL_NAMES: tuple[str, ...] = (TOOL_START_WORKFLOW,)

_START_WORKFLOW_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["workflow", "idea"],
    "properties": {
        "workflow": {
            "type": "string",
            "enum": [HYPOTHESIS_DEVELOPMENT_WORKFLOW],
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

    def start_hypothesis_development(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        actor_id: str,
        ideas: str,
        investigation_id: str | None = None,
        trigger_message_id: str | None = None,
    ) -> Mapping[str, object]:
        """Launch and run one falsification-first hypothesis-development run."""


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
        if workflow != HYPOTHESIS_DEVELOPMENT_WORKFLOW:
            return _failure(f"unknown workflow: {workflow!r}")
        idea = arguments.get("idea")
        if not isinstance(idea, str) or not idea.strip():
            return _failure("idea must be non-empty text")
        investigation_id = arguments.get("investigation_id")
        if investigation_id is not None and (
            not isinstance(investigation_id, str) or not investigation_id.strip()
        ):
            return _failure("investigation_id must be non-empty text when set")
        result = launcher.start_hypothesis_development(
            workspace_id=context.workspace_id,
            chat_id=context.chat_id,
            actor_id=context.actor_id,
            ideas=idea.strip(),
            investigation_id=(
                None if investigation_id is None else investigation_id.strip()
            ),
            # Anchor the execution on the operator message that requested it so
            # the launch never writes a second, duplicate trigger message.
            trigger_message_id=context.trigger_message_id,
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
        "chat; you supply the workflow id and the idea.",
        _START_WORKFLOW_SCHEMA,
    )
    return (RegisteredTool(spec=spec, handler=handle_start_workflow),)


__all__ = [
    "HYPOTHESIS_DEVELOPMENT_WORKFLOW",
    "TOOL_START_WORKFLOW",
    "WORKFLOW_TOOL_NAMES",
    "WorkflowLauncher",
    "workflow_registered_tools",
]
