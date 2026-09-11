"""Provider-agnostic chat reply generation with streamed deltas.

The reply service is the missing link between the chat-owned message surface
and the model providers: on a new user message it assembles the conversation,
streams the model's reply as ``provider.delta`` frames, persists one assistant
``ChatMessage``, and publishes a final ``chat.message`` frame.  The model call
itself is injected as a :data:`ReplyProvider` callable, so the transport and
credentials stay at the composition boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

from thesis.chat.contracts import ChatMessage, MessageRole
from thesis.chat.service import ChatService
from thesis.chat.streaming import CHAT_MESSAGE, PROVIDER_DELTA, InMemoryChatStreamBroker

#: A reply provider maps a conversation to an iterator of reply text chunks.
ReplyProvider = Callable[[Sequence[ChatMessage]], Iterator[str]]

_HISTORY_ROLES = frozenset(
    (MessageRole.USER, MessageRole.ASSISTANT, MessageRole.SYSTEM)
)


def _stream_id() -> str:
    return f"stream-{uuid.uuid4().hex}"


def _sanitize(text: str) -> str:
    """Keep printable characters plus newlines/tabs; drop other control chars."""
    return "".join(
        character for character in text if ord(character) >= 32 or character in "\n\t"
    ).strip()


@dataclass(slots=True)
class ChatReplyService:
    """Generate and stream one assistant reply per user message."""

    chat_service: ChatService
    broker: InMemoryChatStreamBroker
    provider: ReplyProvider
    actor_id: str = "thesis-agent"
    max_history: int = 40
    id_factory: Callable[[], str] = field(default=_stream_id)

    def respond(self, workspace_id: str, chat_id: str) -> ChatMessage | None:
        """Stream and persist the assistant reply; return the durable message.

        Returns ``None`` when there is no conversation or no reply text; a
        provider failure is published as an error delta and never raises into
        the background task boundary.
        """
        history = tuple(
            message
            for message in self.chat_service.list_messages(workspace_id, chat_id)
            if message.role in _HISTORY_ROLES
        )[-self.max_history :]
        if not history:
            return None
        stream_id = self.id_factory()
        self.broker.publish(
            workspace_id,
            chat_id,
            PROVIDER_DELTA,
            {"messageId": stream_id, "role": "assistant", "text": "", "start": True},
        )
        chunks: list[str] = []
        try:
            for chunk in self.provider(history):
                if not chunk:
                    continue
                chunks.append(chunk)
                self.broker.publish(
                    workspace_id,
                    chat_id,
                    PROVIDER_DELTA,
                    {"messageId": stream_id, "text": chunk},
                )
        except Exception as error:  # fail closed at the task boundary
            self.broker.publish(
                workspace_id,
                chat_id,
                PROVIDER_DELTA,
                {"messageId": stream_id, "error": str(error), "done": True},
            )
            return None
        body = _sanitize("".join(chunks))
        if not body:
            self.broker.publish(
                workspace_id,
                chat_id,
                PROVIDER_DELTA,
                {"messageId": stream_id, "done": True},
            )
            return None
        try:
            message = self.chat_service.create_message(
                workspace_id,
                chat_id,
                actor_id=self.actor_id,
                body=body,
                role=MessageRole.ASSISTANT,
            )
        except Exception as error:
            # A reply must never be dropped silently: surface the failure on the
            # stream instead of letting the persistence error escape.
            self.broker.publish(
                workspace_id,
                chat_id,
                PROVIDER_DELTA,
                {"messageId": stream_id, "error": str(error), "done": True},
            )
            return None
        self.broker.publish(
            workspace_id,
            chat_id,
            CHAT_MESSAGE,
            {"message": message.as_dict()},
        )
        self.broker.publish(
            workspace_id,
            chat_id,
            PROVIDER_DELTA,
            {
                "messageId": stream_id,
                "messageIdFinal": message.message_id,
                "done": True,
            },
        )
        return message


__all__ = ["ChatReplyService", "ReplyProvider"]
