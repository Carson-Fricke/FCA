"""The singular storage boundary for canonical incoming session messages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from thesis.agent.messages import SessionMessage
from thesis.agent.runtime.activity import Delivery, activity_data
from thesis.chat.contracts import ChatMessage, MessageRole, TranscriptItemKind
from thesis.chat.service import ChatService

if TYPE_CHECKING:
    from thesis.agent.runtime.delivery import Anchor


class DeliveryError(ValueError):
    """Raised when a delivery request is malformed."""


def write_incoming(
    chat: ChatService,
    anchor: Anchor,
    message: SessionMessage,
    *,
    actor_id: str = "thesis-runtime",
    body: str | None = None,
    role: MessageRole = MessageRole.TOOL,
    kind: TranscriptItemKind = TranscriptItemKind.SUB_AGENT,
    data: Mapping[str, object] | None = None,
    tool_result: Mapping[str, object] | None = None,
    execution_id: str | None = None,
    attachment_ids: tuple[str, ...] = (),
    investigation_ids: tuple[str, ...] = (),
    intent_epoch: int = 0,
) -> ChatMessage:
    """The sole incoming storage boundary, shared by intake and the live inbox."""
    if message.direction != "incoming":
        raise DeliveryError("session delivery requires an incoming message")
    if role not in (MessageRole.USER, MessageRole.TOOL, MessageRole.SYSTEM):
        raise DeliveryError("incoming presentation must not be assistant output")
    payload = dict(data or {})
    key = payload.get("deliveryKey")
    payload.update(
        activity_data(
            Delivery(
                anchor.sub_agent_id, key if isinstance(key, str) else message.message_id
            )
        )
    )
    payload["sessionMessage"] = replace(message, position=0).as_dict()
    return chat.create_message(
        anchor.workspace_id,
        anchor.chat_id,
        message_id=message.message_id,
        actor_id=actor_id,
        body=body if body is not None else message.content.text or "Incoming result",
        role=role,
        kind=kind,
        data=payload,
        tool_result=tool_result,
        execution_id=execution_id,
        attachment_ids=attachment_ids,
        investigation_ids=investigation_ids,
        intent_epoch=intent_epoch,
    )
