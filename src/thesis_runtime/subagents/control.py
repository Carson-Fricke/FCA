"""Thesis control tools.

These tools are presented to an agent in the same model tool catalog as the
harness tools, but they are Thesis-owned: they never reach the harness.  The
model requests a phase or sub-agent completion with a typed contract; the tool
handler constructs that contract and returns field-level errors inside the same
agent turn — the workflow validates the contract and rejects it back to the
agent for repair, and only persisted and validated results are treated as
durable.

Deterministic acceptance is owned by the workflow: a constructed contract is a
proposal, never a transition.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from thesis_runtime.contracts.records import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
    SubAgentRecord,
    SubAgentStatus,
    replace_record,
)
from thesis_runtime.conversations.delivery import Anchor
from thesis_runtime.subagents.child import ChildConversation
from thesis_runtime.subagents.specs import SubAgentSpec

if TYPE_CHECKING:
    from thesis_runtime.conversations.conversation import Conversations

TOOL_REQUEST_PHASE_COMPLETION = "request_phase_completion"
TOOL_REQUEST_SUB_AGENT_COMPLETION = "request_sub_agent_completion"
TOOL_REQUEST_OPERATOR_INPUT = "request_operator_input"
TOOL_REQUEST_WORKFLOW_INPUT = "request_workflow_input"
TOOL_SPAWN_SUB_AGENT = "spawn_sub_agent"

#: The Thesis control tools, in the order they appear in the merged catalog.
CONTROL_TOOL_NAMES: tuple[str, ...] = (
    TOOL_REQUEST_PHASE_COMPLETION,
    TOOL_REQUEST_SUB_AGENT_COMPLETION,
    TOOL_REQUEST_OPERATOR_INPUT,
    TOOL_REQUEST_WORKFLOW_INPUT,
    TOOL_SPAWN_SUB_AGENT,
)


@dataclass(frozen=True, slots=True)
class ControlToolSpec:
    """One Thesis control tool advertised to a model."""

    name: str
    description: str
    input_schema: Mapping[str, object] = field(default_factory=dict)


CONTROL_TOOL_SPECS: tuple[ControlToolSpec, ...] = (
    ControlToolSpec(
        name=TOOL_REQUEST_PHASE_COMPLETION,
        description=(
            "Request completion of the current workflow phase by submitting its "
            "typed completion contract. The runtime resolves the phase and "
            "execution from trusted context; the contract is validated "
            "deterministically and the workflow applies the transition."
        ),
        input_schema={
            "type": "object",
            "properties": {"completion": {"type": "object"}},
            "required": ["completion"],
        },
    ),
    ControlToolSpec(
        name=TOOL_REQUEST_SUB_AGENT_COMPLETION,
        description=(
            "Request terminal completion of this non-phase sub-agent by "
            "submitting its typed result contract."
        ),
        input_schema={
            "type": "object",
            "properties": {"completion": {"type": "object"}},
            "required": ["completion"],
        },
    ),
    ControlToolSpec(
        name=TOOL_REQUEST_OPERATOR_INPUT,
        description=(
            "Publish an agent-authored question to the operator and pause this "
            "agent until the operator replies."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["message"],
        },
    ),
    ControlToolSpec(
        name=TOOL_REQUEST_WORKFLOW_INPUT,
        description=(
            "Publish an agent-authored question to the operator from inside a "
            "workflow phase and pause this agent until the operator replies. "
            "Identical to request_operator_input, and additionally flags the "
            "workflow card so the phase shows as awaiting operator input."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["message"],
        },
    ),
    ControlToolSpec(
        name=TOOL_SPAWN_SUB_AGENT,
        description=(
            "Create a durable chat-attached child sub-agent with an allowed "
            "specification. Returns the child handle immediately; the child "
            "runs asynchronously and its result is delivered to this agent."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "kind": {"type": "string", "enum": ["conversation", "one_shot"]},
                "task": {"type": "string"},
                "allowed_tools": {"type": "array", "items": {"type": "string"}},
                "delegated_budget": {"type": "integer"},
            },
            "required": ["role", "task"],
        },
    ),
)


class StaleCompletionError(RuntimeError):
    """Raised when a completion predates the execution's current intent epoch."""


@dataclass(frozen=True, slots=True)
class SpawnResult:
    """The immediate result of a spawn command: a durable child handle."""

    sub_agent: SubAgentRecord
    invocation: AgentInvocation

    def as_dict(self) -> dict[str, object]:
        return {
            "sub_agent_id": self.sub_agent.sub_agent_id,
            "status": self.sub_agent.status.value,
            "kind": self.sub_agent.kind.value,
            "invocation_id": self.invocation.invocation_id,
        }


@dataclass(frozen=True, slots=True)
class OperatorInputRequest:
    """The durable request published by ``request_operator_input``."""

    sub_agent_id: str
    message: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "subAgentId": self.sub_agent_id,
            "question": self.message,
            "reason": self.reason,
        }


class PhaseCompletionRejected(RuntimeError):
    """Raised when a phase completion cannot be applied and must be repaired.

    The rejection reaches the agent inside the same turn so the completion can
    be re-derived; a phase advances only when the workflow accepts the contract.
    """


type PhaseCompletionApplier = Callable[
    [str, SubAgentRecord, Mapping[str, object]], Mapping[str, object] | None
]

#: Validates a phase contract before it is applied; a returned message is the
#: repair reason the asking agent receives inside the same turn.
type ContractValidator = Callable[
    [str, SubAgentRecord, Mapping[str, object]], str | None
]


class ControlToolService:
    """A thin adapter over the conversations service for the control tools.

    The service owns every semantic: spawning is prompting, a completion is a
    handle call, and a pause is the unified node-input path.  This adapter
    only records the first-class tool invocations and applies the engine-side
    validation callbacks the composition binds.
    """

    def __init__(
        self,
        *,
        conversations: Conversations,
        execution_epochs: Callable[[str, str], int | None] | None = None,
        phase_completions: PhaseCompletionApplier | None = None,
        contract_validator: ContractValidator | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._conversations = conversations
        self._lifecycle = conversations.lifecycle
        self._store = conversations.store
        self._execution_epochs = execution_epochs
        self._phase_completions = phase_completions
        self._contract_validator = contract_validator
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    def spawn(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        spec: SubAgentSpec,
        actor_id: str,
        conversation_id: str,
        prompt: str,
        parent_sub_agent_id: str | None = None,
        workflow_execution_id: str | None = None,
        phase_attempt_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> SpawnResult:
        """Create a durable child with its first prompt and record the spawn.

        Spawning and prompting are one act: the prompt is delivered at the
        child's own anchor, which wakes the child's own turn.  The caller
        never schedules a child turn.
        """
        invocation = self._record_invocation(
            workspace_id=workspace_id,
            chat_id=chat_id,
            conversation_id=conversation_id,
            tool_name=TOOL_SPAWN_SUB_AGENT,
            sub_agent_id=parent_sub_agent_id,
            parent_invocation_id=None,
            workflow_execution_id=workflow_execution_id,
            phase_attempt_id=phase_attempt_id,
            idempotency_key=idempotency_key,
        )
        child = self._conversations.spawn(
            Anchor(workspace_id, chat_id, sub_agent_id=parent_sub_agent_id),
            spec,
            prompt,
            spec.kind,
            actor_id=actor_id,
            spawned_by_invocation_id=invocation.invocation_id,
            attribution={
                "workflow_execution_id": workflow_execution_id,
                "phase_attempt_id": phase_attempt_id,
            },
        )
        record = child.record
        assert record is not None
        return SpawnResult(sub_agent=record, invocation=invocation)

    def request_operator_input(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        message: str,
        reason: str = "",
    ) -> OperatorInputRequest:
        """Publish an agent-authored request and pause the child for input.

        The pause is the unified input path's state transition: the asking
        child enters ``needs_input`` through its request-input handle, so the
        answer accepted for that child resumes the same conversation.  There
        is no second, direct lifecycle call.
        """
        if not isinstance(message, str) or not message.strip():
            raise ValueError("request_operator_input requires a non-empty message")
        conversation = self._conversations.get(workspace_id, sub_agent_id)
        if conversation is None:
            raise RuntimeError("sub-agent does not exist")
        conversation.request_input()
        return OperatorInputRequest(
            sub_agent_id=sub_agent_id,
            message=message.strip(),
            reason=reason.strip(),
        )

    def complete_sub_agent(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        result: Mapping[str, object],
    ) -> SubAgentRecord:
        """Mark a non-phase child completed and deliver its result to the caller.

        The child is the caller's own conversation, so its constructed result
        is persisted at the caller anchor and scheduled through the one
        delivery path every other datum uses.
        """
        return self._complete_child(workspace_id, sub_agent_id, result=result)

    def complete_phase(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        completion: Mapping[str, object],
        expected_attempt_id: str | None,
    ) -> SubAgentRecord:
        """Apply a phase completion through its workflow and return the node.

        The workflow is the only owner of a phase completion: it validates the
        contract, commits the transition, closes the phase node, and records
        the completion contract as that node's result.  This adapter only runs
        the engine-side validation callback, applies the transition, and
        delivers the finished result to the return anchor exactly once.  A node
        the workflow does not own is rejected rather than silently completed.
        """
        record = self._lifecycle.get(workspace_id, sub_agent_id)
        if record is None:
            raise RuntimeError("sub-agent does not exist")
        parent = (
            self._lifecycle.get(workspace_id, record.parent_sub_agent_id)
            if record.parent_sub_agent_id
            else None
        )
        if not (
            record.phase_executor
            and record.phase
            and record.phase_attempt_id
            and record.workflow_execution_id
            and parent is not None
            and parent.role == "workflow-node"
            and parent.workflow_execution_id == record.workflow_execution_id
            and parent.chat_id == record.chat_id
        ):
            raise PhaseCompletionRejected(
                "request_phase_completion requires a workflow phase node; "
                "use request_sub_agent_completion for a sub-agent"
            )
        if (
            expected_attempt_id is None
            or record.phase_attempt_id != expected_attempt_id
        ):
            raise PhaseCompletionRejected(
                "completion does not belong to the current phase attempt"
            )
        self._reject_stale_completion(workspace_id, record, completion)
        if self._contract_validator is not None:
            # An authoritative validation (for example a harness compile of
            # the authored program) runs before any transition commits, so
            # its failure reaches the agent as a repairable tool result.
            failure = self._contract_validator(workspace_id, record, completion)
            if failure:
                raise PhaseCompletionRejected(failure)
        self._reject_stale_completion(workspace_id, record, completion)
        if self._phase_completions is None:
            raise PhaseCompletionRejected(
                "no workflow is available to apply this completion"
            )
        if self._phase_completions(workspace_id, record, completion) is None:
            raise PhaseCompletionRejected(
                "no workflow is available to apply this completion"
            )
        completed = self._lifecycle.get(workspace_id, sub_agent_id)
        if completed is None or completed.status is not SubAgentStatus.COMPLETED:
            raise PhaseCompletionRejected(
                "the workflow did not complete this phase node; the phase remains open"
            )
        return self._child(workspace_id, sub_agent_id).deliver_completed_result()

    def _complete_child(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        result: Mapping[str, object],
    ) -> SubAgentRecord:
        child = self._child(workspace_id, sub_agent_id)
        record = child.record
        if record is None:
            raise RuntimeError("sub-agent does not exist")
        if record.phase_executor:
            raise PhaseCompletionRejected(
                "phase executors must request phase completion"
            )
        self._reject_stale_completion(workspace_id, record, result)
        return child.complete(result)

    def _child(self, workspace_id: str, sub_agent_id: str) -> ChildConversation:
        """Resolve one child conversation handle, failing when it is absent."""
        resolved = self._conversations.get(workspace_id, sub_agent_id)
        if not isinstance(resolved, ChildConversation):
            raise RuntimeError("sub-agent does not exist")
        return resolved

    def _reject_stale_completion(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        result: Mapping[str, object],
    ) -> None:
        """Reject a completion whose stamped epoch is no longer current.

        The rejection reaches the agent inside the same turn so it can
        re-derive the contract; conversations and queued turns never check the
        epoch themselves.
        """
        if record.workflow_execution_id is None:
            return
        raw_epoch = result.get("intentEpoch")
        stamped = raw_epoch if type(raw_epoch) is int else None
        current = (
            self._execution_epochs(workspace_id, record.workflow_execution_id)
            if self._execution_epochs is not None
            else self._conversations.chat.get_execution(
                workspace_id, record.workflow_execution_id
            ).intent_epoch
        )
        if current is not None and current == stamped:
            return
        raise StaleCompletionError(
            f"completion is stamped with intent epoch {stamped} but the "
            f"execution is at epoch {current}; re-derive the contract"
        )

    def _record_invocation(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        conversation_id: str,
        tool_name: str,
        sub_agent_id: str | None,
        parent_invocation_id: str | None,
        workflow_execution_id: str | None,
        phase_attempt_id: str | None,
        idempotency_key: str | None,
    ) -> AgentInvocation:
        now = self._clock()
        invocation = AgentInvocation(
            workspace_id=workspace_id,
            created_at=now,
            created_by="thesis-control",
            invocation_id=self._id_factory("invocation"),
            chat_id=chat_id,
            conversation_id=conversation_id,
            kind=AgentInvocationKind.TOOL,
            state=AgentInvocationState.SUCCEEDED,
            sub_agent_id=sub_agent_id,
            parent_invocation_id=parent_invocation_id,
            workflow_execution_id=workflow_execution_id,
            phase_attempt_id=phase_attempt_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            updated_at=now,
            root_conversation_id=chat_id,
            parent_conversation_id=None if conversation_id == chat_id else chat_id,
        )
        return self._store.save_invocation(invocation)


def set_result_ref(record: SubAgentRecord, result_ref: str) -> SubAgentRecord:
    """Attach an immutable result reference to a completed sub-agent record."""
    return replace_record(record, result_ref=result_ref, updated_at=datetime.now(UTC))


__all__ = [
    "CONTROL_TOOL_NAMES",
    "CONTROL_TOOL_SPECS",
    "ControlToolService",
    "ControlToolSpec",
    "OperatorInputRequest",
    "SpawnResult",
    "StaleCompletionError",
    "TOOL_REQUEST_OPERATOR_INPUT",
    "TOOL_REQUEST_WORKFLOW_INPUT",
    "TOOL_REQUEST_PHASE_COMPLETION",
    "TOOL_REQUEST_SUB_AGENT_COMPLETION",
    "TOOL_SPAWN_SUB_AGENT",
    "set_result_ref",
]
