"""Launch any registered workflow using its own validated input schema."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.datum import DatumType, datum_from_result
from thesis_runtime.tools.registry import RegisteredTool, ToolExecution
from thesis_runtime.workflows.registry import WorkflowRegistry
from thesis_runtime.workflows.spec import WorkflowContext

TOOL_START_WORKFLOW = "start_workflow"
WORKFLOW_TOOL_NAMES = (TOOL_START_WORKFLOW,)


class WorkflowLauncher(Protocol):
    @property
    def workflows(self) -> WorkflowRegistry: ...


def workflow_registered_tools(
    launcher: WorkflowLauncher, context: ToolContext
) -> tuple[RegisteredTool, ...]:
    registry = launcher.workflows
    branches = [
        {
            "type": "object",
            "required": ["workflow", "input"],
            "additionalProperties": False,
            "properties": {
                "workflow": {"const": name},
                "input": dict(registry.resolve(name).launch_schema),
            },
        }
        for name in registry.ids()
    ]
    if not branches:
        return ()

    def launch(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        workflow = str(arguments["workflow"])
        inputs = arguments["input"]
        if not isinstance(inputs, Mapping):
            raise ValueError("workflow input must be an object")
        trusted = WorkflowContext(
            workspace_id=context.workspace_id,
            chat_id=context.chat_id,
            actor_id=context.actor_id,
            trigger_message_id=context.trigger_message_id,
            intent_epoch=context.intent_epoch,
            anchor_sub_agent_id=context.sub_agent_id,
        )
        result = registry.launch(workflow, trusted, inputs)
        return ToolExecution(
            ok=True,
            structured=result,
            text=f"Workflow '{workflow}' started.",
            datums=(
                datum_from_result(
                    DatumType.WORKFLOW_EXECUTION.value,
                    ok=True,
                    structured=result,
                    text=f"Workflow '{workflow}' started.",
                ),
            ),
        )

    return (
        RegisteredTool(
            descriptor={
                "name": TOOL_START_WORKFLOW,
                "description": "Start a registered workflow using its declared input. The host supplies trusted execution context.",
                "parameters": {"oneOf": branches},
            },
            handler=launch,
        ),
    )
