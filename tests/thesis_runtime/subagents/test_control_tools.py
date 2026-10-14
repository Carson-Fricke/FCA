"""Completion tools are scoped to the node kind that owns them.

A workflow phase node returns its phase contract; every other node returns a
plain sub-agent result. The two completion routes are disjoint by
construction, so a non-workflow node can never call the phase tool (and the
engine's phase completion never has to guess what a missing workflow means).
"""

from __future__ import annotations

from typing import cast

from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.subagents.control_tools import (
    ControlToolRuntime,
    control_registered_tools,
)


def _context(
    *,
    sub_agent_id: str | None = None,
    workflow_execution_id: str | None = None,
    phase_executor: bool = False,
) -> ToolContext:
    return ToolContext(
        workspace_id="ws",
        chat_id="chat",
        conversation_id="conv",
        actor_id="actor",
        sub_agent_id=sub_agent_id,
        workflow_execution_id=workflow_execution_id,
        phase_executor=phase_executor,
    )


def _names(context: ToolContext) -> set[str]:
    runtime = cast(ControlToolRuntime, object())
    return {tool.name for tool in control_registered_tools(runtime, context)}


def test_chat_scope_receives_no_completion_tool() -> None:
    names = _names(_context())
    assert "request_operator_input" in names
    assert "request_phase_completion" not in names
    assert "request_sub_agent_completion" not in names


def test_workflow_phase_scope_receives_only_the_phase_completion() -> None:
    names = _names(
        _context(
            sub_agent_id="phase-node",
            workflow_execution_id="exec-1",
            phase_executor=True,
        )
    )
    assert "request_workflow_input" in names
    assert "request_phase_completion" in names
    assert "request_operator_input" not in names
    assert "request_sub_agent_completion" not in names


def test_non_workflow_node_scope_receives_only_the_sub_agent_completion() -> None:
    names = _names(_context(sub_agent_id="child"))
    assert "request_operator_input" in names
    assert "request_sub_agent_completion" in names
    assert "request_workflow_input" not in names
    assert "request_phase_completion" not in names
