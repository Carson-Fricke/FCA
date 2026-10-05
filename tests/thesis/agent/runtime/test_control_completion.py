"""``complete_phase`` is a workflow-only route and never degrades silently.

The workflow owns phase completion: it validates the contract, commits the
transition, and closes the node. A node without a workflow execution is not a
phase node, so the phase tool rejects it instead of completing it as if it were
a plain sub-agent.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from thesis.agent.runtime.control import ControlToolService, PhaseCompletionRejected
from thesis.agent.runtime.conversation import Conversations


class _Lifecycle:
    def __init__(self, record: object) -> None:
        self._record = record

    def get(self, workspace_id: str, sub_agent_id: str) -> object:
        return self._record


def _service(record: object) -> ControlToolService:
    conversations = SimpleNamespace(
        lifecycle=_Lifecycle(record), store=SimpleNamespace()
    )
    return ControlToolService(
        conversations=cast(Conversations, conversations),
    )


def test_complete_phase_rejects_a_non_workflow_node() -> None:
    service = _service(
        SimpleNamespace(
            workflow_execution_id=None, parent_sub_agent_id=None, phase_executor=False
        )
    )
    with pytest.raises(PhaseCompletionRejected, match="workflow phase node"):
        service.complete_phase("ws", "node", completion={"target": "next"})


def test_complete_phase_requires_an_applier_when_a_workflow_is_bound() -> None:
    service = _service(
        SimpleNamespace(
            workflow_execution_id="exec-1",
            parent_sub_agent_id="workflow",
            phase_executor=True,
            phase="analysis",
            phase_attempt_id="attempt",
            role="workflow-node",
            chat_id="chat",
        )
    )
    service._execution_epochs = lambda workspace, execution: 0
    with pytest.raises(PhaseCompletionRejected, match="no workflow is available"):
        service.complete_phase(
            "ws", "node", completion={"target": "next", "intentEpoch": 0}
        )
