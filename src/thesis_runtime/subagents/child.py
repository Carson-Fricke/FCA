"""A conversation plus child semantics, by composition.

A :class:`ChildConversation` holds an inner conversation it delegates every
operation to, and adds only: a ``returns_to`` anchor (who receives the
result), ``complete`` / ``fail`` / ``result`` /
``children``, cancellation returns and transcript-based return settlement,
and one turn-end hook — "does a turn that ends
in text complete me?"  The hook is the child's creation-time policy:
:attr:`OneShotPolicy` completes the child and delivers its answer to the
return anchor; :attr:`ConversationalPolicy` keeps the conversation open.

``returns_to`` is an anchor, never a conversation object: delivering at a
conversation anchor schedules that conversation's turn, while delivering at a
record anchor is durable-only, because the reacting party already ran.  The
same wrapper serves every child without special-casing either.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

from thesis_runtime.contracts.datum import Datum
from thesis_runtime.contracts.messages import SessionMessage
from thesis_runtime.conversations import (
    ChatMessage,
    MessageRole,
    SubAgentRecord,
)
from thesis_runtime.conversations.delivery import (
    Anchor,
    DeliveryReceipt,
)

if TYPE_CHECKING:
    from thesis_runtime.conversations.conversation import (
        Conversation,
        Conversations,
    )


class TurnEndPolicy(Protocol):
    """The single turn-end hook: does a turn that ends in text complete me?"""

    def turn_ended(self, record: SubAgentRecord, result: object) -> None: ...


class OneShotPolicy:
    """Completes the child when a turn ends in text.

    The child's final answer is its result: the turn ends, the record
    completes with that answer, and the result is delivered to the return
    anchor.
    """

    def __init__(self, service: Conversations, returns_to: Anchor) -> None:
        self._service = service
        self._returns_to = returns_to

    def turn_ended(self, record: SubAgentRecord, result: object) -> None:
        from thesis_runtime.conversations import SubAgentStatus
        from thesis_runtime.sessions.session import SessionResult

        if (
            not isinstance(result, SessionResult)
            or result.status != "completed"
            or result.completion is not None
            or not result.content.strip()
        ):
            return
        current = self._service.store.get_sub_agent(
            record.workspace_id, record.sub_agent_id
        )
        if current is None or current.status is not SubAgentStatus.RUNNING:
            return
        completed = self._service.lifecycle.complete(
            record.workspace_id,
            record.sub_agent_id,
            result={"summary": result.content},
        )
        self._service.delivery.deliver_child_result(self._returns_to, completed)


class ConversationalPolicy:
    """Never completes on text: the conversation stays open for input."""

    def turn_ended(self, record: SubAgentRecord, result: object) -> None:
        del record, result


def has_pending_child_returns(messages: Sequence[SessionMessage]) -> bool:
    """Join existing spawn-handle receipts to delivered terminal child results.

    Spawn returns a handle immediately; only the later child-result message
    settles that delegated work. The caller supplies one scoped transcript.
    """
    handles: set[str] = set()
    returned: set[str] = set()
    for message in messages:
        if message.direction != "incoming":
            continue
        result = message.content.result or {}
        if result.get("kind") == "child_result":
            child_id = result.get("childSubAgentId")
            if isinstance(child_id, str):
                returned.add(child_id)
        datums = result.get("datums", ())
        if not isinstance(datums, list | tuple):
            continue
        for datum in datums:
            if (
                not isinstance(datum, Mapping)
                or datum.get("datumType") != "sub_agent_handle"
            ):
                continue
            structured = datum.get("structured")
            if datum.get("ok") is True and isinstance(structured, Mapping):
                child_id = structured.get("sub_agent_id")
                if isinstance(child_id, str):
                    handles.add(child_id)
    return bool(handles - returned)


class ChildConversation:
    """One conversation plus the return layer, delegating every internal."""

    def __init__(
        self,
        service: Conversations,
        inner: Conversation,
        returns_to: Anchor,
        policy: TurnEndPolicy,
    ) -> None:
        self._service = service
        self._inner = inner
        self._returns_to = returns_to
        self._policy = policy

    @property
    def returns_to(self) -> Anchor:
        """Return the anchor that receives this child's result."""
        return self._returns_to

    @property
    def record(self) -> SubAgentRecord | None:
        """Return the child's durable record, when it exists."""
        return self._service.store.get_sub_agent(
            self.workspace_id, self.conversation_id
        )

    @property
    def conversation_id(self) -> str:
        """Return the child's single identity: its conversation id."""
        return self._inner.conversation_id

    @property
    def workspace_id(self) -> str:
        """Return the workspace the child lives in."""
        return self._inner.workspace_id

    @property
    def chat_id(self) -> str:
        """Return the chat that contains and stores the child."""
        return self._inner.chat_id

    # -- return semantics -----------------------------------------------------

    def complete(self, result: Mapping[str, object]) -> SubAgentRecord:
        """Mark the child completed and deliver its result to the return anchor."""
        self._service.lifecycle.complete(
            self.workspace_id, self.conversation_id, result=result
        )
        return self.deliver_completed_result()

    def deliver_completed_result(self) -> SubAgentRecord:
        """Deliver an already-completed child's result to the return anchor once.

        The phase-completion route completes the node inside the workflow (the
        transition owns the result), so this handle owns only the return
        delivery and must not re-complete the record.
        """
        record = self._service.store.get_sub_agent(
            self.workspace_id, self.conversation_id
        )
        if record is None:
            raise RuntimeError("sub-agent does not exist")
        from thesis_runtime.conversations import SubAgentStatus

        if record.status is not SubAgentStatus.COMPLETED:
            raise RuntimeError("only a completed child can deliver a completed result")
        self._service.delivery.deliver_child_result(self._returns_to, record)
        return record

    def fail(self, reason: str) -> SubAgentRecord:
        """Mark the child failed and deliver its failure to the return anchor."""
        failed = self._service.lifecycle.fail(
            self.workspace_id, self.conversation_id, result={"failure": reason}
        )
        self._service.delivery.deliver_child_result(self._returns_to, failed)
        return failed

    @property
    def result(self) -> Mapping[str, object] | None:
        """Return the child's terminal result, when it has one."""
        record = self._service.store.get_sub_agent(
            self.workspace_id, self.conversation_id
        )
        return None if record is None else record.result

    def children(self) -> tuple[SubAgentRecord, ...]:
        """Return the child's direct children."""
        return self._service.children(self.workspace_id, self.conversation_id)

    def cancel(self) -> tuple[str, ...]:
        """Cancel the child and every non-terminal descendant of it.

        The affordance is idempotent and history-preserving: terminal records
        are left untouched, and only ids that actually transitioned to
        ``cancelled`` are returned, in root-first tree order.
        """
        cancelled = self._service.lifecycle.cancel_subtree(
            self.workspace_id, self.conversation_id
        )
        record = self.record
        if record is not None:
            self._service.delivery.deliver_child_result(self._returns_to, record)
        return cancelled

    # -- everything else delegates to the inner conversation ------------------

    def send(
        self,
        body: str,
        actor_id: str,
        *,
        role: MessageRole = MessageRole.USER,
        attachment_ids: Sequence[str] = (),
        references: Mapping[str, tuple[str, ...]] | None = None,
        execution_id: str | None = None,
        intent_epoch: int = 0,
    ) -> ChatMessage:
        """Persist one message at the child's anchor and wake the child."""
        return self._inner.send(
            body,
            actor_id,
            role=role,
            attachment_ids=attachment_ids,
            references=references,
            execution_id=execution_id,
            intent_epoch=intent_epoch,
        )

    def deliver(
        self,
        datum: Datum,
        *,
        body: str | None = None,
        data: Mapping[str, object] | None = None,
        execution_id: str | None = None,
        actor_id: str = "thesis-runtime",
        idempotency_key: str | None = None,
        schedule: bool = True,
    ) -> DeliveryReceipt:
        """Persist one datum at the child's anchor, then wake the child."""
        return self._inner.deliver(
            datum,
            body=body,
            data=data,
            execution_id=execution_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            schedule=schedule,
        )

    def request_input(self) -> SubAgentRecord:
        """Open the child's input state so an answer may resume it."""
        return self._inner.request_input()

    def status(self) -> str:
        """Return the child's durable status."""
        return self._inner.status()

    def in_flight(self) -> bool:
        """Return whether a turn is currently running for the child."""
        return self._inner.in_flight()

    def transcript(self) -> tuple[ChatMessage, ...]:
        """Return the child's own anchored messages, in order."""
        return self._inner.transcript()

    def run_turn(self) -> object:
        """Run the child's next turn (the worker body)."""
        return self._inner.run_turn()

    def turn_ended(self, result: object) -> None:
        """Apply the policy resolved from this child's durable record."""
        record = self.record
        if record is not None:
            self._policy.turn_ended(record, result)


__all__ = [
    "ChildConversation",
    "ConversationalPolicy",
    "OneShotPolicy",
    "TurnEndPolicy",
]
