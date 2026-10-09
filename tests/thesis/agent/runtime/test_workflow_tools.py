"""The workflow-launch handle carries the resolved workflow identity.

Clients resolve an execution's declared topology from the handle's
``workflowId``/``version``; a handle without them makes a client invent an id
and query a workflow that was never registered.
"""

from __future__ import annotations

from collections.abc import Mapping

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.datum import DatumType
from thesis.agent.runtime.workflow_tools import (
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    TOOL_START_WORKFLOW,
    workflow_registered_tools,
)
from thesis.workflows.core.registry import WorkflowRegistry
from thesis.workflows.core.spec import WorkflowContext
from thesis.workflows.registry import build_workflow_registry


class _Launcher:
    """Minimal structural ``WorkflowLauncher`` over a real registry."""

    def __init__(self, workflows: WorkflowRegistry) -> None:
        self.workflows = workflows


def _context() -> ToolContext:
    return ToolContext(
        workspace_id="ws",
        chat_id="chat",
        conversation_id="conv",
        actor_id="actor",
        trigger_message_id="msg-1",
    )


def test_start_workflow_handle_carries_workflow_identity() -> None:
    launched: list[str] = []

    def execute(
        context: WorkflowContext,
        *,
        ideas: str,
        investigation_id: str | None = None,
        launch_options: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        launched.append(ideas)
        return {"executionId": "exec-1", "phase": "requested", "summary": "started"}

    registry = build_workflow_registry(hypothesis_execute=execute)
    (tool,) = workflow_registered_tools(_Launcher(registry), _context())

    execution = tool.handler(
        {
            "workflow": HYPOTHESIS_DEVELOPMENT_WORKFLOW,
            "idea": "an edge",
            "launch_options": {},
        }
    )

    spec = registry.resolve(HYPOTHESIS_DEVELOPMENT_WORKFLOW).spec
    assert tool.name == TOOL_START_WORKFLOW
    assert launched == ["an edge"]
    assert execution.ok
    assert execution.structured["workflowId"] == spec.workflow_id
    assert execution.structured["version"] == spec.version
    assert len(execution.datums) == 1
    datum = execution.datums[0]
    assert datum.datum_type == DatumType.WORKFLOW_EXECUTION.value
    assert datum.structured["workflowId"] == spec.workflow_id
    assert datum.structured["version"] == spec.version
