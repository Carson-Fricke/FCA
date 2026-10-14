"""Node lifecycle.

Every workflow execution owns one durable workflow node parented to the anchor
that launched it.  :class:`WorkflowNodeManager` creates, resolves, and closes
that node.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from thesis_runtime.contracts.records import (
    SubAgentKind,
    SubAgentRecord,
    WorkflowExecutionState,
)
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.execution.events import ExecutionEventType
from thesis_runtime.execution.sub_agents import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SubAgentContextSpec,
    append_sub_agent_event,
)
from thesis_runtime.subagents.lifecycle import SubAgentLifecycle
from thesis_runtime.subagents.specs import SubAgentSpec
from thesis_runtime.workflows.spec import WorkflowContext


def _execution_id(context: WorkflowContext) -> str:
    if context.execution_id is None:
        raise ValueError("workflow context requires an execution id")
    return context.execution_id


class WorkflowNodeManager:
    """Own the durable workflow node of each execution."""

    def __init__(
        self,
        chat: ChatService,
        lifecycle: SubAgentLifecycle | None,
        *,
        role: str,
        actor_id: str,
        initial_phase: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._chat = chat
        self._lifecycle = lifecycle
        self._role = role
        self._actor_id = actor_id
        self._initial_phase = initial_phase
        self._clock = clock
        self._nodes: dict[str, str] = {}

    def create(
        self, context: WorkflowContext, anchor_sub_agent_id: str | None = None
    ) -> SubAgentRecord | None:
        """Create the execution's node as a child of the launching anchor."""
        if self._lifecycle is None:
            return None
        execution_id = _execution_id(context)
        node = self._lifecycle.spawn(
            context.workspace_id,
            context.chat_id,
            spec=SubAgentSpec(role=self._role, kind=SubAgentKind.ONE_SHOT),
            actor_id=self._actor_id,
            parent_sub_agent_id=anchor_sub_agent_id,
            workflow_execution_id=execution_id,
        )
        self._nodes[execution_id] = node.sub_agent_id
        append_sub_agent_event(
            self._chat.event_store,
            execution_id=execution_id,
            workspace_id=context.workspace_id,
            event_type=ExecutionEventType.SUB_AGENT_SPAWNED,
            occurred_at=self._clock(),
            sub_agent_id=node.sub_agent_id,
            parent_sub_agent_id=anchor_sub_agent_id,
            phase=self._initial_phase,
            role=self._role,
            context=SubAgentContextSpec(),
            accepts_input=False,
            intent_epoch=context.intent_epoch,
        )
        return node

    def resolve(self, context: WorkflowContext) -> str | None:
        """Resolve the durable workflow node owning this execution's phases."""
        execution_id = context.execution_id
        if execution_id is None:
            return None
        cached = self._nodes.get(execution_id)
        if cached is not None:
            return cached
        if self._lifecycle is None:
            return None
        node = self._lifecycle.workflow_node(context.workspace_id, execution_id)
        if node is None:
            return None
        self._nodes[execution_id] = node.sub_agent_id
        return node.sub_agent_id

    def finish(
        self, context: WorkflowContext, final_state: WorkflowExecutionState
    ) -> None:
        """Close the execution's durable workflow node when the run ends."""
        if self._lifecycle is None:
            return
        node_id = self.resolve(context)
        if node_id is None:
            return
        record = self._lifecycle.get(context.workspace_id, node_id)
        if record is None:
            return
        completed = final_state is WorkflowExecutionState.COMPLETED
        if completed:
            self._lifecycle.complete(context.workspace_id, node_id)
        else:
            self._lifecycle.fail(context.workspace_id, node_id)
        append_sub_agent_event(
            self._chat.event_store,
            execution_id=_execution_id(context),
            workspace_id=context.workspace_id,
            event_type=(
                ExecutionEventType.SUB_AGENT_COMPLETED
                if completed
                else ExecutionEventType.SUB_AGENT_FAILED
            ),
            occurred_at=self._clock(),
            sub_agent_id=node_id,
            parent_sub_agent_id=record.parent_sub_agent_id,
            phase=self._initial_phase,
            role=self._role,
            context=SubAgentContextSpec(),
            accepts_input=False,
            intent_epoch=context.intent_epoch,
            extra={
                "frozen": True,
                "status": STATUS_COMPLETED if completed else STATUS_FAILED,
            },
        )
