"""The workflow-launch handle carries the resolved workflow identity.

Clients resolve an execution's declared topology from the handle's
``workflowId``/``version``; a handle without them makes a client invent an id
and query a workflow that was never registered.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from thesis.extensions.workflows import (
    HYPOTHESIS_DEVELOPMENT_WORKFLOW_ID as HYPOTHESIS_DEVELOPMENT_WORKFLOW,
)
from thesis.extensions.workflows import build_workflow_registry
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.datum import DatumType
from thesis_runtime.tools.registry import SessionToolRegistry
from thesis_runtime.tools.workflows import (
    TOOL_START_WORKFLOW,
    workflow_registered_tools,
)
from thesis_runtime.workflows.registry import WorkflowRegistry
from thesis_runtime.workflows.spec import WorkflowContext


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
        inputs: Mapping[str, object],
    ) -> Mapping[str, object]:
        launched.append(str(inputs["idea"]))
        return {"executionId": "exec-1", "phase": "requested", "summary": "started"}

    registry = build_workflow_registry(hypothesis_execute=execute)
    (tool,) = workflow_registered_tools(_Launcher(registry), _context())

    execution = SessionToolRegistry([tool]).dispatch(
        TOOL_START_WORKFLOW,
        {
            "workflow": HYPOTHESIS_DEVELOPMENT_WORKFLOW,
            "input": {
                "idea": "an edge",
            },
        },
        context=_context(),
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


@pytest.mark.parametrize(
    "inputs", [{"idea": ""}, {"idea": "an edge", "launch_options": {}}, {}]
)
def test_invalid_or_outdated_launch_inputs_are_rejected(
    inputs: dict[str, object],
) -> None:
    def execute(
        context: WorkflowContext, inputs: Mapping[str, object]
    ) -> Mapping[str, object]:
        pytest.fail("invalid input must not launch")

    registry = build_workflow_registry(hypothesis_execute=execute)
    result = SessionToolRegistry(
        workflow_registered_tools(_Launcher(registry), _context())
    ).dispatch(
        TOOL_START_WORKFLOW,
        {"workflow": HYPOTHESIS_DEVELOPMENT_WORKFLOW, "input": inputs},
        context=_context(),
    )
    assert not result.ok
    assert result.failure and result.failure["certainty"] == "not_started"
