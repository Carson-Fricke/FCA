"""Node lifecycle and node-turn delivery.

Every workflow execution owns one durable workflow node parented to the anchor
that launched it.  :class:`WorkflowNodeManager` creates, resolves, and closes
that node.  :class:`NodeTurnDelivery` delivers operator questions as node
turns: the question is persisted at the node's anchor and schedules nothing,
because the accepted operator answer is the single scheduling point.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from thesis.agent.runtime.datum import DatumType, datum_from_result
from thesis.agent.runtime.delivery import (
    Anchor,
    DeliveryReceipt,
    DeliveryService,
    NodeInputService,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.chat.contracts import (
    MessageRole,
    SubAgentKind,
    SubAgentRecord,
    TranscriptItemKind,
    WorkflowExecutionState,
)
from thesis.chat.service import ChatService
from thesis.execution.events import ExecutionEventType
from thesis.execution.sub_agents import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SubAgentContextSpec,
    append_sub_agent_event,
)
from thesis.workflows.core.spec import WorkflowContext


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


class NodeTurnDelivery:
    """Deliver operator questions as durable turns at a node's anchor."""

    def __init__(
        self,
        *,
        chat: ChatService,
        delivery: DeliveryService,
        node_inputs: NodeInputService,
        actor_id: str,
    ) -> None:
        self._chat = chat
        self._delivery = delivery
        self._node_inputs = node_inputs
        self._actor_id = actor_id

    def sequence(self, context: WorkflowContext) -> int:
        """Return the retry-stable sequence number for the next request.

        The sequence is the number of operator questions already delivered for
        the execution plus one, so a retried pause resolves to the key of the
        message it already delivered instead of duplicating the turn.
        """
        execution_id = _execution_id(context)
        return (
            sum(
                1
                for message in self._chat.list_messages(
                    context.workspace_id, context.chat_id
                )
                if message.execution_id == execution_id
                and message.data is not None
                and message.data.get("kind") == "operator_question"
            )
            + 1
        )

    def deliver_question(
        self,
        context: WorkflowContext,
        *,
        sub_agent_id: str,
        phase: str,
        body: str,
        question: str | None = None,
        fields: Mapping[str, object] | None = None,
    ) -> DeliveryReceipt:
        """Persist one question as the node's assistant turn, scheduling none.

        The question has no answer yet, so a delivery-time schedule could only
        produce the workflow's unanswered-request no-op.  The answer accepted
        through :meth:`~thesis.agent.runtime.delivery.NodeInputService.accept`
        is the single scheduling point.  A re-delivered question resolves to
        the durable message instead of duplicating the turn.
        """
        execution_id = _execution_id(context)
        payload: dict[str, object] = {
            "kind": "operator_question",
            "phase": phase,
            "turn": "assistant",
        }
        if question is not None:
            payload["question"] = question
        if fields:
            payload.update(fields)
        # The delivery key identifies the pause occurrence, not the question
        # text: two cycles can pause with the same authored request, and each
        # must deliver its own turn.
        sequence = self.sequence(context)
        payload["sequence"] = sequence
        datum = datum_from_result(
            DatumType.OPERATOR_QUESTION.value,
            ok=True,
            structured=dict(payload),
            text=body,
        )
        return self._delivery.deliver(
            Anchor(context.workspace_id, context.chat_id, sub_agent_id=sub_agent_id),
            datum,
            body=body,
            role=MessageRole.ASSISTANT,
            kind=TranscriptItemKind.SUB_AGENT,
            data=payload,
            execution_id=execution_id,
            actor_id=self._actor_id,
            idempotency_key=f"operator-request-{execution_id}-{sequence}",
            schedule=False,
        )

    def deliver_request(
        self,
        context: WorkflowContext,
        *,
        sub_agent_id: str,
        phase: str,
        body: str,
        question: str | None = None,
        fields: Mapping[str, object] | None = None,
        open_node_input: bool,
    ) -> None:
        """Deliver one operator request and optionally open its node input.

        The node's durable record enters ``needs_input`` through the unified
        node-input path so the answer accepted for that node resumes the same
        record under the node's operator policy; the question delivery itself
        schedules nothing.
        """
        if open_node_input:
            self._node_inputs.request_input(context.workspace_id, sub_agent_id)
        self.deliver_question(
            context,
            sub_agent_id=sub_agent_id,
            phase=phase,
            body=body,
            question=question,
            fields=fields,
        )
