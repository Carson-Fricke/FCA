"""The singular storage boundary for canonical incoming session messages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from thesis_runtime.contracts.activity import Delivery, activity_data
from thesis_runtime.contracts.datum import Datum, tool_result_payload
from thesis_runtime.contracts.errors import DeliveryError
from thesis_runtime.contracts.messages import SessionMessage
from thesis_runtime.contracts.records import (
    ChatMessage,
    MessageRole,
    TranscriptItemKind,
)
from thesis_runtime.conversations.service import ChatService

if TYPE_CHECKING:
    from thesis_runtime.conversations.delivery import Anchor


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
    references: Mapping[str, tuple[str, ...]] | None = None,
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
        references=references,
        intent_epoch=intent_epoch,
    )


def presentation_fields(
    message: SessionMessage,
    *,
    data: Mapping[str, object] | None,
    datums: Sequence[Datum],
    idempotency_key: str | None,
    tool_result: Mapping[str, object] | None,
    body: str | None,
) -> tuple[dict[str, object], Mapping[str, object] | None, str | None]:
    """Prepare detached presentation fields without granting authority or writing."""
    payload = dict(data or {})
    if datums:
        payload["datums"] = [datum.as_dict() for datum in datums]
    if idempotency_key is not None:
        payload["deliveryKey"] = idempotency_key
    content = message.content
    result = content.result
    if result is not None:
        detached = content.as_dict()["result"]
        if tool_result is None:
            correlation = content.correlation
            primary_type = content.source
            raw_datums = (
                detached.get("datums") if isinstance(detached, Mapping) else None
            )
            if (
                isinstance(raw_datums, Sequence)
                and raw_datums
                and isinstance(raw_datums[0], Mapping)
            ):
                candidate = raw_datums[0].get("datumType")
                if isinstance(candidate, str) and candidate.strip():
                    primary_type = candidate
            tool_result = tool_result_payload(
                operation=result.get("operation"),
                call_id=correlation.request_id if correlation else None,
                tool=correlation.tool_name if correlation else None,
                datum_type=primary_type,
                ok=bool(result.get("ok")),
                structured=detached.get("structured", {})
                if isinstance(detached, Mapping)
                and isinstance(detached.get("structured"), Mapping)
                else {},
                text=content.text,
                summary=content.text
                or f"{correlation.tool_name if correlation else content.source} result",
                failure=detached.get("failure")
                if isinstance(detached, Mapping)
                and isinstance(detached.get("failure"), Mapping)
                else None,
            )
        if body is None and isinstance(tool_result, Mapping):
            summary = tool_result.get("summary")
            text = tool_result.get("text")
            if isinstance(summary, str) and summary.strip():
                body = summary
            elif isinstance(text, str) and text.strip():
                body = text
    return payload, tool_result, body
