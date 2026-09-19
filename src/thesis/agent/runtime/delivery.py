"""One delivery path for datums and answers addressed to a conversation anchor.

A conversation anchor is either the root chat (``sub_agent_id is None``) or one
sub-agent's node conversation.  DEC-Q5 requires one delivery path parameterized
by anchor: persist the datum as a message at the anchor, then schedule a turn at
that anchor.  Delivery is durable before it is scheduled and idempotent under
retry, keyed by a delivery key persisted inside the message payload so the
dedupe survives reload without a new durable record field.

The same module owns the node-input path: an answer addressed to a node is
authorized by the node's operator policy (human-only by default, DEC-Q3) before
it is persisted and scheduled.  The answering actor and its kind are built by
the trusted caller, never by a model or a request body.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from thesis.agent.runtime.datum import Datum, DatumType
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import (
    AnsweringActor,
    OperatorPolicyDecision,
    OperatorPolicyRegistry,
)
from thesis.agent.runtime.store import PersistenceSubAgentStore, SubAgentStore
from thesis.chat.contracts import (
    ChatMessage,
    MessageRole,
    SubAgentRecord,
    SubAgentStatus,
    TranscriptItemKind,
)
from thesis.chat.service import ChatService

#: Scheduler signature: ``(workspace_id, chat_id, sub_agent_id) -> None``.  The
#: empty string denotes the root chat anchor, matching the chat turn seam.
TurnScheduler = Callable[[str, str, str], None]

#: Scheduler id used for the root chat anchor.
ROOT_ANCHOR_ID = ""

#: Message data key carrying the durable idempotency key of a delivery.
DELIVERY_KEY_FIELD = "deliveryKey"


class DeliveryError(ValueError):
    """Raised when a delivery request is malformed."""


class NodeInputNotFound(LookupError):
    """Raised when an answer is addressed to an unknown node."""


class NodeInputNotAccepted(RuntimeError):
    """Raised when a node does not accept input in its current state."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryError(f"{name} must be non-empty text")
    return value.strip()


@dataclass(frozen=True, slots=True)
class Anchor:
    """The conversation a datum or answer belongs to.

    ``sub_agent_id`` is ``None`` for the root chat and set for one node.
    """

    workspace_id: str
    chat_id: str
    sub_agent_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "workspace_id", _require_text(self.workspace_id, "workspace_id")
        )
        object.__setattr__(self, "chat_id", _require_text(self.chat_id, "chat_id"))
        if self.sub_agent_id is not None:
            object.__setattr__(
                self,
                "sub_agent_id",
                _require_text(self.sub_agent_id, "sub_agent_id"),
            )

    @property
    def schedule_id(self) -> str:
        """Return the scheduler argument for this anchor."""
        return self.sub_agent_id or ROOT_ANCHOR_ID

    @property
    def is_root(self) -> bool:
        """Return whether this anchor is the top-level chat."""
        return self.sub_agent_id is None

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, JSON-ready anchor."""
        return {
            "workspaceId": self.workspace_id,
            "chatId": self.chat_id,
            "subAgentId": self.sub_agent_id,
        }


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """The durable outcome of one delivery."""

    message: ChatMessage
    idempotency_key: str
    scheduled: bool
    duplicate: bool

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, JSON-ready receipt."""
        return {
            "messageId": self.message.message_id,
            "idempotencyKey": self.idempotency_key,
            "scheduled": self.scheduled,
            "duplicate": self.duplicate,
        }


def delivery_key(anchor: Anchor, datum: Datum) -> str:
    """Derive the idempotency key of one datum at one anchor."""
    canonical = json.dumps(
        datum.as_dict(), ensure_ascii=True, sort_keys=True, default=str
    )
    digest = hashlib.sha256(
        f"{anchor.sub_agent_id or ROOT_ANCHOR_ID}|{canonical}".encode()
    ).hexdigest()
    return f"datum-{digest[:32]}"


def find_delivered(
    chat: ChatService, anchor: Anchor, idempotency_key: str
) -> ChatMessage | None:
    """Return the durable message carrying ``idempotency_key``, or ``None``."""
    for message in chat.list_messages(anchor.workspace_id, anchor.chat_id):
        data = message.data
        if data is not None and data.get(DELIVERY_KEY_FIELD) == idempotency_key:
            return message
    return None


def _datum_payload(datum: Datum) -> dict[str, object]:
    return {
        "datumType": datum.datum_type,
        "ok": datum.ok,
        "structured": dict(datum.structured),
        "text": datum.text,
        "summary": datum.summary,
        "failure": None if datum.failure is None else dict(datum.failure),
    }


def _default_body(datum: Datum) -> str:
    return (
        (datum.summary or "").strip()
        or datum.text.strip()
        or f"{datum.datum_type} result"
    )


class DeliveryService:
    """Persist a datum at its anchor, then schedule that anchor's turn."""

    def __init__(
        self,
        chat: ChatService,
        *,
        schedule: TurnScheduler | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._chat = chat
        self._schedule = schedule
        self._clock = clock

    @property
    def scheduler(self) -> TurnScheduler | None:
        """Return the injected turn scheduler, if any."""
        return self._schedule

    def deliver(
        self,
        anchor: Anchor,
        datum: Datum,
        *,
        body: str | None = None,
        kind: TranscriptItemKind = TranscriptItemKind.SUB_AGENT,
        role: MessageRole = MessageRole.TOOL,
        data: Mapping[str, object] | None = None,
        execution_id: str | None = None,
        actor_id: str = "thesis-runtime",
        idempotency_key: str | None = None,
        tool_result: Mapping[str, object] | None = None,
        schedule: bool = True,
    ) -> DeliveryReceipt:
        """Persist ``datum`` at ``anchor`` and schedule one turn there.

        A re-delivered datum resolves to the durable message already carrying
        its idempotency key; it neither duplicates the message nor schedules a
        second turn.  ``schedule=False`` persists a datum that cannot yet
        produce a runnable turn -- an operator question before any answer
        exists -- so nothing is dispatched until the answer's own accept path
        schedules the turn; the receipt reports ``scheduled=False``.
        """
        key = (
            delivery_key(anchor, datum)
            if idempotency_key is None
            else _require_text(idempotency_key, "idempotency_key")
        )
        existing = find_delivered(self._chat, anchor, key)
        if existing is not None:
            return DeliveryReceipt(
                message=existing,
                idempotency_key=key,
                scheduled=False,
                duplicate=True,
            )
        payload = _datum_payload(datum)
        payload.update(dict(data or {}))
        payload["subAgentId"] = anchor.sub_agent_id
        payload[DELIVERY_KEY_FIELD] = key
        message = self._chat.create_message(
            anchor.workspace_id,
            anchor.chat_id,
            actor_id=actor_id,
            body=body or _default_body(datum),
            role=role,
            kind=kind,
            tool_result=payload if tool_result is None else tool_result,
            data=payload,
            execution_id=execution_id,
        )
        scheduled = schedule and self._schedule is not None
        if schedule and self._schedule is not None:
            self._schedule(anchor.workspace_id, anchor.chat_id, anchor.schedule_id)
        return DeliveryReceipt(
            message=message,
            idempotency_key=key,
            scheduled=scheduled,
            duplicate=False,
        )

    def deliver_child_result(
        self, anchor: Anchor, child: SubAgentRecord, *, schedule: bool = True
    ) -> DeliveryReceipt:
        """Deliver a child's terminal (or current) result to its anchor.

        ``schedule=False`` records the result without waking the anchor: a phase
        agent's parent is the workflow node, which is a record rather than a
        conversation, so its continuation is the engine reaction that already
        ran in the completing tool call.
        """
        text = (
            f"Sub-agent {child.role} ({child.sub_agent_id}) ended: "
            f"{child.status.value}."
        )
        datum = Datum(
            datum_type=DatumType.SUB_AGENT_STATUS.value,
            ok=child.status is SubAgentStatus.COMPLETED,
            structured={
                "childSubAgentId": child.sub_agent_id,
                "status": child.status.value,
            },
            text=text,
            summary=text,
        )
        return self.deliver(
            anchor,
            datum,
            body=text,
            data={
                "childSubAgentId": child.sub_agent_id,
                "kind": "child_result",
                "status": child.status.value,
                "result": None if child.result is None else dict(child.result),
                "resultRef": child.result_ref,
            },
            execution_id=child.workflow_execution_id,
            schedule=schedule,
        )


@dataclass(frozen=True, slots=True)
class NodeInputReceipt:
    """The durable outcome of one answer accepted for a node."""

    message: ChatMessage
    decision: OperatorPolicyDecision
    scheduled: bool
    duplicate: bool

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, JSON-ready receipt."""
        return {
            "messageId": self.message.message_id,
            "decision": self.decision.as_dict(),
            "scheduled": self.scheduled,
            "duplicate": self.duplicate,
        }


class NodeInputService:
    """Accept an answer addressed to a node under its operator policy.

    This is the node-input path DEC-Q3 governs.  The answerer's identity and
    kind arrive as an :class:`AnsweringActor` built by the trusted caller; the
    message is persisted with the policy provenance and only then is the node's
    turn scheduled.
    """

    def __init__(
        self,
        *,
        chat: ChatService,
        store: SubAgentStore | None = None,
        policies: OperatorPolicyRegistry | None = None,
        schedule: TurnScheduler | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._chat = chat
        self._store = store or PersistenceSubAgentStore(chat.persistence)
        self._policies = policies or OperatorPolicyRegistry()
        self._lifecycle = SubAgentLifecycle(chat, store=self._store)
        self._schedule = schedule
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    @property
    def policies(self) -> OperatorPolicyRegistry:
        """Return the active operator policy registry."""
        return self._policies

    def request_input(
        self,
        workspace_id: str,
        sub_agent_id: str,
    ) -> SubAgentRecord:
        """Open one node's input state so an answer may resume it.

        This is the one pause path: the question's state lives on the asking
        node, and the same node's answer path (:meth:`accept`) consumes the
        input and returns the node to ``running``.
        """
        record = self._store.get_sub_agent(workspace_id, sub_agent_id)
        if record is None:
            raise NodeInputNotFound(f"sub-agent was not found: {sub_agent_id}")
        return self._lifecycle.request_input(workspace_id, sub_agent_id)

    def accept(
        self,
        workspace_id: str,
        chat_id: str,
        sub_agent_id: str,
        *,
        body: str,
        actor: AnsweringActor,
        idempotency_key: str | None = None,
    ) -> NodeInputReceipt:
        """Authorize, persist, and schedule one answer for a node."""
        message_body = _require_text(body, "body")
        record = self._store.get_sub_agent(workspace_id, sub_agent_id)
        if record is None or record.chat_id != chat_id:
            raise NodeInputNotFound(f"sub-agent was not found: {sub_agent_id}")
        if not self._lifecycle.accepts_unsolicited_input(record):
            raise NodeInputNotAccepted(
                f"sub-agent {sub_agent_id} is not accepting input"
            )
        decision = self._policies.authorize_node_input(record, actor)
        decision.require()
        key = (
            self._id_factory("input")
            if idempotency_key is None
            else _require_text(idempotency_key, "idempotency_key")
        )
        anchor = Anchor(workspace_id, chat_id, sub_agent_id)
        existing = find_delivered(self._chat, anchor, key)
        if existing is not None:
            return NodeInputReceipt(
                message=existing,
                decision=decision,
                scheduled=False,
                duplicate=True,
            )
        message = self._chat.create_message(
            workspace_id,
            chat_id,
            actor_id=actor.actor_id,
            body=message_body,
            role=MessageRole.USER,
            kind=TranscriptItemKind.SUB_AGENT,
            data={
                "subAgentId": sub_agent_id,
                "parentSubAgentId": record.parent_sub_agent_id,
                "kind": "operator_input",
                "actorKind": actor.kind.value,
                "operatorPolicy": decision.as_dict(),
                DELIVERY_KEY_FIELD: key,
            },
        )
        if record.status is SubAgentStatus.NEEDS_INPUT:
            self._lifecycle.resume(workspace_id, sub_agent_id)
        scheduled = self._schedule is not None
        if self._schedule is not None:
            self._schedule(workspace_id, chat_id, sub_agent_id)
        return NodeInputReceipt(
            message=message,
            decision=decision,
            scheduled=scheduled,
            duplicate=False,
        )


__all__ = [
    "DELIVERY_KEY_FIELD",
    "ROOT_ANCHOR_ID",
    "Anchor",
    "DeliveryError",
    "DeliveryReceipt",
    "DeliveryService",
    "NodeInputNotAccepted",
    "NodeInputNotFound",
    "NodeInputReceipt",
    "NodeInputService",
    "TurnScheduler",
    "delivery_key",
    "find_delivered",
]
