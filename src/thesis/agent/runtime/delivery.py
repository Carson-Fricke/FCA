"""One delivery path for datums and answers addressed to a conversation anchor.

A conversation anchor is either the root chat (``sub_agent_id is None``) or one
sub-agent's node conversation.  DEC-Q5 requires one delivery path parameterized
by anchor: persist the datum as a message at the anchor, then schedule a turn at
that anchor.  Delivery is durable before it is scheduled and idempotent under
retry, keyed by a delivery key persisted inside the message payload so the
dedupe survives reload without a new durable record field.

The receipt an accepted node answer returns (:class:`NodeInputReceipt`) and the
answering actor it carries (:class:`~thesis.agent.runtime.policy.AnsweringActor`)
live here too: the answering actor and its kind are built by the trusted caller,
never by a model or a request body.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from thesis.agent.messages import MessageContent, SessionMessage, from_record
from thesis.agent.runtime.activity import Delivery, activity_data, decode_activity
from thesis.agent.runtime.datum import Datum, DatumType, tool_result_payload
from thesis.agent.runtime.incoming import DeliveryError, write_incoming
from thesis.agent.runtime.policy import (
    OperatorPolicyDecision,
)
from thesis.chat.contracts import (
    ChatMessage,
    MessageRole,
    RecordNotFound,
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
    """Return the durable message carrying ``idempotency_key``, or ``None``.

    Delivery identity is deterministic from the anchor and key, so the durable
    record's identity is the idempotency check: one point read, no history scan.
    """
    try:
        message = chat.get_message(
            anchor.workspace_id, _delivery_id(anchor, idempotency_key)
        )
    except RecordNotFound:
        return None
    if (
        message.chat_id == anchor.chat_id
        and decode_activity(message).sub_agent_id == anchor.sub_agent_id
    ):
        return message
    return None


def _delivery_id(anchor: Anchor, key: str) -> str:
    scope = json.dumps([anchor.workspace_id, anchor.chat_id, anchor.sub_agent_id, key])
    return f"message-{uuid.uuid5(uuid.NAMESPACE_URL, scope).hex}"


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
        schedule_input: Callable[[ChatMessage], None] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        deliver_session_message: Callable[..., SessionMessage] | None = None,
    ) -> None:
        self._chat = chat
        self._schedule = schedule
        self._schedule_input = schedule_input
        self._clock = clock
        self._deliver_session_message = deliver_session_message

    def deliver(
        self,
        anchor: Anchor,
        datum: Datum | None = None,
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

        A delivery without a datum carries no datum payload: the caller must
        supply an explicit ``idempotency_key`` and ``body``, and the persisted
        message is built from ``data``/``role``/``kind`` alone (no tool result
        unless one is explicitly passed).  The key is what makes the delivery
        idempotent, so it is never derived for a datum-less delivery.
        """
        if datum is None:
            if idempotency_key is None:
                raise DeliveryError(
                    "a delivery without a datum requires an explicit idempotency_key"
                )
            if body is None:
                raise DeliveryError(
                    "a delivery without a datum requires an explicit body"
                )
        key = (
            delivery_key(anchor, datum)
            if datum is not None and idempotency_key is None
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
        if datum is None:
            payload: dict[str, object] = dict(data or {})
            message_body = _require_text(body, "body")
            message_tool_result = tool_result
        else:
            payload = _datum_payload(datum)
            payload.update(dict(data or {}))
            message_body = body or _default_body(datum)
            message_tool_result = (
                tool_result_payload(
                    call_id=None,
                    tool=None,
                    datum_type=datum.datum_type,
                    ok=datum.ok,
                    structured=datum.structured,
                    summary=datum.summary,
                    text=datum.text,
                    failure=datum.failure,
                )
                if tool_result is None
                else tool_result
            )
        payload.update(activity_data(Delivery(anchor.sub_agent_id, key)))
        canonical = SessionMessage(
            _delivery_id(anchor, key),
            "incoming",
            MessageContent(
                text=message_body,
                source=datum.datum_type
                if datum is not None
                else "user"
                if role is MessageRole.USER
                else "delivery",
                result=payload if datum is not None else None,
            ),
        )
        writer = self._deliver_session_message
        if writer is None:
            message = write_incoming(
                self._chat,
                anchor,
                canonical,
                actor_id=actor_id,
                body=message_body,
                role=role,
                kind=kind,
                tool_result=message_tool_result,
                data=payload,
                execution_id=execution_id,
            )
        else:
            durable = writer(
                anchor,
                canonical,
                actor_id=actor_id,
                body=message_body,
                role=role,
                kind=kind,
                tool_result=message_tool_result,
                data=payload,
                execution_id=execution_id,
                schedule=False,
            )
            message = self._chat.get_message(anchor.workspace_id, durable.message_id)
            if from_record(message) != durable:
                raise DeliveryError(
                    "delivery writer returned a different durable message"
                )
        scheduled = schedule and (
            self._schedule is not None or self._schedule_input is not None
        )
        if schedule and self._schedule_input is not None:
            self._schedule_input(message)
        elif schedule and self._schedule is not None:
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
        current = self._chat.persistence.get(
            SubAgentRecord, child.workspace_id, child.sub_agent_id
        )
        if current is not None:
            child = current
        if child.status not in (
            SubAgentStatus.COMPLETED,
            SubAgentStatus.FAILED,
            SubAgentStatus.CANCELLED,
        ):
            raise DeliveryError("only a terminal child can deliver a result")
        if child.workspace_id != anchor.workspace_id or child.chat_id != anchor.chat_id:
            raise DeliveryError("child result belongs to another conversation scope")
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
    "TurnScheduler",
    "delivery_key",
    "find_delivered",
]
