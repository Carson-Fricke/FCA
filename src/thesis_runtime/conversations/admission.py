"""Storage admission rules shared by conversation persistence adapters.

Input positions are increasing storage cursors, not contiguous ordinals. An
adapter must commit the assigned position, message and runnable reference
together. Outputs never advance a conversation's consumed-input cursor.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime

from thesis_runtime.contracts.activity import is_external_input
from thesis_runtime.contracts.errors import DeliveryError
from thesis_runtime.contracts.records import (
    ChatMessage,
    ChatState,
    ChatThread,
    InvalidLifecycleTransition,
    RecordNotFound,
    replace_record,
)


def require_live_chat(chat: ChatThread | None) -> ChatThread:
    """Reject missing/deleted roots without permitting their resurrection."""
    if chat is None or chat.state is ChatState.DELETED:
        raise RecordNotFound("conversation was not found")
    return chat


def require_admission_chat(chat: ChatThread | None) -> ChatThread:
    """Admission and lifecycle changes share the authoritative root revision."""
    chat = require_live_chat(chat)
    if chat.state is not ChatState.ACTIVE:
        raise InvalidLifecycleTransition("messages require an active conversation")
    return chat


def existing_admission(previous: ChatMessage, proposed: ChatMessage) -> ChatMessage:
    """Join an identical canonical input; reject identity or content conflicts."""
    if (previous.chat_id, previous.conversation_id) != (
        proposed.chat_id,
        proposed.conversation_id,
    ):
        raise DeliveryError("message identity belongs to another conversation")
    canonical = (proposed.data or {}).get("sessionMessage")
    stored = (previous.data or {}).get("sessionMessage")
    if (
        not is_external_input(proposed)
        or not isinstance(canonical, Mapping)
        or not isinstance(stored, Mapping)
    ):
        raise DeliveryError("message identity already exists")
    if previous.actor_id != proposed.actor_id or any(
        stored.get(name) != canonical.get(name) for name in ("direction", "content")
    ):
        raise DeliveryError("message identity already has different content")
    return previous


def position_message(
    message: ChatMessage, position: int, *, arrival_sequence: int
) -> ChatMessage:
    """Assign the storage cursor to the neutral input and its record envelope."""
    message = replace_record(
        message, data={**dict(message.data or {}), "arrivalSequence": arrival_sequence}
    )
    if not is_external_input(message):
        return message
    canonical = (message.data or {}).get("sessionMessage")
    return replace_record(
        message,
        input_position=position,
        data={
            **dict(message.data or {}),
            "sessionMessage": {**dict(canonical), "position": position},
        }
        if isinstance(canonical, Mapping)
        else message.data,
    )


def message_bookkeeping(chat: ChatThread, message: ChatMessage) -> ChatThread:
    return replace_record(
        chat,
        links=chat.links.add(
            references=message.references,
            workflow_execution_ids=(message.execution_id,)
            if message.execution_id
            else (),
        ),
        updated_at=max(chat.updated_at or chat.created_at, message.created_at),
    )


def select_message_window(
    messages: Sequence[ChatMessage],
    *,
    after_position: int | None,
    after_message_id: str | None,
) -> tuple[ChatMessage, ...]:
    """Complete scoped history after a checkpoint, or new incoming arrivals."""
    ordered = sorted(messages, key=message_order_key)
    if after_position is not None:
        return tuple(row for row in ordered if row.input_position > after_position)
    if after_message_id is None:
        return tuple(ordered)
    for index, row in enumerate(ordered):
        if row.message_id == after_message_id:
            return tuple(ordered[index + 1 :])
    raise RecordNotFound(
        "context checkpoint boundary was not found in this conversation"
    )


def arrival_sequence(message: ChatMessage) -> int:
    """Return storage-owned ordering metadata, absent on unadmitted records."""
    value = (message.data or {}).get("arrivalSequence", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def message_order_key(message: ChatMessage) -> tuple[int, datetime]:
    """Order admitted records by storage sequence, independently of host clocks."""
    return arrival_sequence(message), message.created_at


def ordered_messages(messages: Sequence[ChatMessage]) -> tuple[ChatMessage, ...]:
    """Order incoming peers without moving evidence across outputs."""
    messages = sorted(messages, key=message_order_key)
    start = 0
    while start < len(messages):
        if not is_external_input(messages[start]):
            start += 1
            continue
        end = start + 1
        while end < len(messages) and is_external_input(messages[end]):
            end += 1
        messages[start:end] = sorted(
            messages[start:end], key=lambda message: message.input_position
        )
        start = end
    return tuple(messages)
