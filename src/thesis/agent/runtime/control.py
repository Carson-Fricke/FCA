"""Thesis control tools and the phase-completion handshake.

These tools are presented to an agent in the same model tool catalog as the
harness tools, but they are Thesis-owned: they never reach the harness.  The
model requests a phase or sub-agent completion with a typed contract; the tool
handler constructs that contract, returns field-level errors inside the same
agent turn, and only persisted and validated results are treated as durable.

The retry semantics are exact and nested:

* one agent turn permits ``construction_attempts`` failed constructions, each
  returned to the model as a tool result;
* a turn that ends without an accepted completion gets a durable repair turn,
  bounded by ``repair_turns``;
* exhausting the repair turns escalates to the operator.

Deterministic acceptance (advance/loopback/remain/fail) is owned by the workflow
gate, not this module: a constructed contract is a proposal, never a transition.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar

from thesis.agent.runtime.delivery import Anchor, DeliveryService, NodeInputService
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.spawn import SubAgentSpawner
from thesis.agent.runtime.specs import (
    DEFAULT_CONSTRUCTION_ATTEMPTS,
    DEFAULT_REPAIR_TURNS,
    SubAgentSpec,
)
from thesis.agent.runtime.store import SubAgentStore
from thesis.chat.contracts import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
    SubAgentRecord,
    replace_record,
)

TOOL_REQUEST_PHASE_COMPLETION = "request_phase_completion"
TOOL_REQUEST_SUB_AGENT_COMPLETION = "request_sub_agent_completion"
TOOL_REQUEST_OPERATOR_INPUT = "request_operator_input"
TOOL_REQUEST_WORKFLOW_INPUT = "request_workflow_input"
TOOL_SPAWN_SUB_AGENT = "spawn_sub_agent"
TOOL_AWAIT_SUB_AGENT = "await_sub_agent"

#: The Thesis control tools, in the order they appear in the merged catalog.
CONTROL_TOOL_NAMES: tuple[str, ...] = (
    TOOL_REQUEST_PHASE_COMPLETION,
    TOOL_REQUEST_SUB_AGENT_COMPLETION,
    TOOL_REQUEST_OPERATOR_INPUT,
    TOOL_REQUEST_WORKFLOW_INPUT,
    TOOL_SPAWN_SUB_AGENT,
    TOOL_AWAIT_SUB_AGENT,
)

_T = TypeVar("_T")


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
            "deterministically and the phase gate decides the transition."
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
    ControlToolSpec(
        name=TOOL_AWAIT_SUB_AGENT,
        description=(
            "Wait (bounded) for a spawned child sub-agent to finish and return "
            "its result. If it is still running when the timeout elapses, the "
            "current state is returned: end your turn and the child's result "
            "will be delivered here when it finishes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sub_agent_id": {"type": "string"},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["sub_agent_id"],
        },
    ),
)


class CompletionStatus(StrEnum):
    """The outcome of one completion-contract submission or repair decision."""

    CONSTRUCTED = "constructed"
    CONSTRUCTION_FAILED = "construction_failed"
    EXHAUSTED = "exhausted"
    REPAIR = "repair"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class CompletionOutcome:
    """The typed result of a completion submission or repair boundary."""

    status: CompletionStatus
    errors: tuple[str, ...] = ()
    value: object | None = None
    construction_attempts_used: int = 0
    repair_turn: int = 0

    @property
    def accepted(self) -> bool:
        return self.status is CompletionStatus.CONSTRUCTED

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "errors": list(self.errors),
            "attemptsUsed": self.construction_attempts_used,
            "repairTurn": self.repair_turn,
        }


class PhaseCompletionProtocol:
    """Nested construction/repair budgets for one phase attempt.

    Three failed constructions are permitted per agent turn.  A turn that ends
    without an accepted completion consumes one of two repair turns; exhausting
    them escalates to the operator.  On operator response the caller starts a
    fresh epoch with :meth:`reset_epoch`.
    """

    def __init__(
        self,
        *,
        phase_attempt_id: str,
        construction_attempts: int = DEFAULT_CONSTRUCTION_ATTEMPTS,
        repair_turns: int = DEFAULT_REPAIR_TURNS,
    ) -> None:
        if construction_attempts < 1:
            raise ValueError("construction_attempts must be at least one")
        if repair_turns < 0:
            raise ValueError("repair_turns must not be negative")
        self._phase_attempt_id = phase_attempt_id
        self._construction_attempts = construction_attempts
        self._repair_turns = repair_turns
        self._epoch = 0
        self._repair_turn = 0
        self._attempts_used = 0
        self._last_errors: tuple[str, ...] = ()
        self._accepted = False

    @property
    def phase_attempt_id(self) -> str:
        return self._phase_attempt_id

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def repair_turn(self) -> int:
        return self._repair_turn

    @property
    def attempts_used(self) -> int:
        return self._attempts_used

    @property
    def accepted(self) -> bool:
        return self._accepted

    def submit(
        self,
        payload: object,
        construct: Callable[[object], _T],
    ) -> CompletionOutcome:
        """Construct one submitted contract, reporting errors in-turn."""
        if self._accepted:
            raise RuntimeError("a completion protocol cannot accept a second contract")
        if self._attempts_used >= self._construction_attempts:
            return CompletionOutcome(
                status=CompletionStatus.EXHAUSTED,
                errors=self._last_errors,
                construction_attempts_used=self._attempts_used,
                repair_turn=self._repair_turn,
            )
        self._attempts_used += 1
        try:
            value = construct(payload)
        except Exception as error:  # noqa: BLE001 - a bad contract is a normal turn
            self._last_errors = _error_lines(error)
            status = (
                CompletionStatus.EXHAUSTED
                if self._attempts_used >= self._construction_attempts
                else CompletionStatus.CONSTRUCTION_FAILED
            )
            return CompletionOutcome(
                status=status,
                errors=self._last_errors,
                construction_attempts_used=self._attempts_used,
                repair_turn=self._repair_turn,
            )
        self._accepted = True
        self._last_errors = ()
        return CompletionOutcome(
            status=CompletionStatus.CONSTRUCTED,
            value=value,
            construction_attempts_used=self._attempts_used,
            repair_turn=self._repair_turn,
        )

    def begin_repair_turn(self) -> CompletionOutcome:
        """Close an unaccepted turn and allow a durable repair turn, or escalate."""
        if self._accepted:
            return CompletionOutcome(
                status=CompletionStatus.CONSTRUCTED,
                construction_attempts_used=self._attempts_used,
                repair_turn=self._repair_turn,
            )
        if self._repair_turn >= self._repair_turns:
            return CompletionOutcome(
                status=CompletionStatus.ESCALATE,
                errors=self._last_errors,
                construction_attempts_used=self._attempts_used,
                repair_turn=self._repair_turn,
            )
        self._repair_turn += 1
        self._attempts_used = 0
        return CompletionOutcome(
            status=CompletionStatus.REPAIR,
            errors=self._last_errors,
            construction_attempts_used=self._attempts_used,
            repair_turn=self._repair_turn,
        )

    def reset_epoch(self) -> None:
        """Start a fresh operator-directed epoch with full budgets."""
        self._epoch += 1
        self._repair_turn = 0
        self._attempts_used = 0
        self._last_errors = ()
        self._accepted = False


def _error_lines(error: BaseException) -> tuple[str, ...]:
    text = str(error).strip()
    return (text,) if text else (type(error).__name__,)


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
class AwaitResult:
    """A child's current state or terminal result, returned to the caller."""

    sub_agent_id: str
    status: str
    terminal: bool
    result: Mapping[str, object] | None = None
    result_ref: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "sub_agent_id": self.sub_agent_id,
            "status": self.status,
            "terminal": self.terminal,
            "result": None if self.result is None else dict(self.result),
            "result_ref": self.result_ref,
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


class ControlToolService:
    """Executes the Thesis control tools against durable runtime ports.

    The service owns no delivery semantics of its own: an operator pause goes
    through the unified :class:`~thesis.agent.runtime.delivery.NodeInputService`
    path, and a completed node's result reaches its caller through the unified
    :class:`~thesis.agent.runtime.delivery.DeliveryService` path.
    """

    def __init__(
        self,
        *,
        lifecycle: SubAgentLifecycle,
        store: SubAgentStore,
        inputs: NodeInputService,
        delivery: DeliveryService,
        execution_epochs: Callable[[str, str], int | None] | None = None,
        phase_completions: PhaseCompletionApplier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        await_timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.25,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._store = store
        self._inputs = inputs
        self._delivery = delivery
        self._execution_epochs = execution_epochs
        self._phase_completions = phase_completions
        self._spawner = SubAgentSpawner(
            lifecycle=lifecycle, delivery=delivery, clock=clock
        )
        self._clock = clock
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._await_timeout = await_timeout_seconds
        self._poll_interval = poll_interval_seconds
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
        spawned = self._spawner.spawn(
            workspace_id,
            chat_id,
            spec=spec,
            actor_id=actor_id,
            prompt=prompt,
            parent_sub_agent_id=parent_sub_agent_id,
            spawned_by_invocation_id=invocation.invocation_id,
            workflow_execution_id=workflow_execution_id,
            phase_attempt_id=phase_attempt_id,
        )
        return SpawnResult(sub_agent=spawned.record, invocation=invocation)

    def await_sub_agent(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> AwaitResult:
        """Wait for a child to finish, then return its terminal state.

        The wait is bounded: when the child is still running at the timeout,
        the current state is returned so the caller can end its turn — the
        child's result is delivered to the caller's anchor when it finishes.
        """
        budget = self._await_timeout if timeout_seconds is None else timeout_seconds
        deadline = self._monotonic() + max(0.0, min(float(budget), self._await_timeout))
        while True:
            result = self._await_once(workspace_id, sub_agent_id)
            if result.terminal or self._monotonic() >= deadline:
                return result
            self._sleep(self._poll_interval)

    def _await_once(self, workspace_id: str, sub_agent_id: str) -> AwaitResult:
        """Return a child's current or terminal state to its caller."""
        record = self._lifecycle.get(workspace_id, sub_agent_id)
        if record is None:
            raise RuntimeError("sub-agent does not exist")
        terminal = record.status.value in {"completed", "failed", "cancelled"}
        return AwaitResult(
            sub_agent_id=record.sub_agent_id,
            status=record.status.value,
            terminal=terminal,
            result=record.result,
            result_ref=record.result_ref,
        )

    def request_operator_input(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        message: str,
        reason: str = "",
    ) -> OperatorInputRequest:
        """Publish an agent-authored request and pause the node for input.

        The pause is the unified input path's state transition: the asking node
        enters ``needs_input`` through :meth:`NodeInputService.request_input`, so
        the answer accepted for that node resumes the same record.  There is no
        second, direct lifecycle call.
        """
        if not isinstance(message, str) or not message.strip():
            raise ValueError("request_operator_input requires a non-empty message")
        self._inputs.request_input(workspace_id, sub_agent_id)
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

        The node is the caller's own conversation, so its constructed result is
        persisted at the caller anchor and scheduled through the one delivery
        path every other datum uses.
        """
        return self._complete_node(workspace_id, sub_agent_id, result=result)

    def complete_phase(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        completion: Mapping[str, object],
    ) -> SubAgentRecord:
        """Mark a phase node completed with its constructed completion contract.

        The persisted result is a proposal, never a transition: the owning
        deterministic workflow gate remains the only thing that advances a
        phase.
        """
        record = self._lifecycle.get(workspace_id, sub_agent_id)
        if record is None:
            raise RuntimeError("sub-agent does not exist")
        if (
            record.workflow_execution_id is not None
            and self._phase_completions is not None
        ):
            applied = self._phase_completions(workspace_id, record, completion)
            if applied is None:
                raise PhaseCompletionRejected(
                    "no workflow is available to apply this completion"
                )
        return self._complete_node(workspace_id, sub_agent_id, result=completion)

    def _complete_node(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        result: Mapping[str, object],
    ) -> SubAgentRecord:
        record = self._lifecycle.get(workspace_id, sub_agent_id)
        if record is None:
            raise RuntimeError("sub-agent does not exist")
        self._reject_stale_completion(workspace_id, record, result)
        record = self._lifecycle.complete(workspace_id, sub_agent_id, result=result)
        self._delivery.deliver_child_result(
            Anchor(
                workspace_id,
                record.chat_id,
                sub_agent_id=record.parent_sub_agent_id,
            ),
            record,
            # A phase agent's parent is the workflow node: a record, not a
            # conversation.  Its continuation already ran in this tool call, so
            # only a non-phase child wakes a real agent parent.
            schedule=record.phase is None,
        )
        return record

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
        if self._execution_epochs is None or record.workflow_execution_id is None:
            return
        raw_epoch = result.get("intentEpoch")
        stamped = raw_epoch if isinstance(raw_epoch, int) else 0
        current = self._execution_epochs(workspace_id, record.workflow_execution_id)
        if current is None or current == stamped:
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
        )
        return self._store.save_invocation(invocation)


def set_result_ref(record: SubAgentRecord, result_ref: str) -> SubAgentRecord:
    """Attach an immutable result reference to a completed sub-agent record."""
    return replace_record(record, result_ref=result_ref, updated_at=datetime.now(UTC))


__all__ = [
    "CONTROL_TOOL_NAMES",
    "CONTROL_TOOL_SPECS",
    "CompletionOutcome",
    "CompletionStatus",
    "ControlToolService",
    "ControlToolSpec",
    "OperatorInputRequest",
    "PhaseCompletionProtocol",
    "SpawnResult",
    "AwaitResult",
    "StaleCompletionError",
    "TOOL_AWAIT_SUB_AGENT",
    "TOOL_REQUEST_OPERATOR_INPUT",
    "TOOL_REQUEST_WORKFLOW_INPUT",
    "TOOL_REQUEST_PHASE_COMPLETION",
    "TOOL_REQUEST_SUB_AGENT_COMPLETION",
    "TOOL_SPAWN_SUB_AGENT",
    "set_result_ref",
]
