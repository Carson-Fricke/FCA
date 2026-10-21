"""Public handle for one anchored conversation; delegates effects to its service."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from thesis_runtime.contracts.activity import ModelOutput, decode_activity
from thesis_runtime.contracts.datum import Datum
from thesis_runtime.contracts.messages import MessageContent, SessionMessage
from thesis_runtime.contracts.records import (
    ChatMessage,
    MessageRole,
    SubAgentRecord,
    TranscriptItemKind,
)
from thesis_runtime.conversations.delivery import (
    ROOT_ANCHOR_ID,
    Anchor,
    DeliveryError,
    DeliveryReceipt,
)
from thesis_runtime.conversations.execution import NodeTurnError
from thesis_runtime.sessions.session import SessionResult
from thesis_runtime.subagents.policy import AnsweringActor

if TYPE_CHECKING:
    from thesis_runtime.conversations.conversation import Conversations
_LOGGER = logging.getLogger(__name__)


class Conversation:
    """The only owner of one anchored transcript and its turns."""

    def __init__(self, service: Conversations, anchor: Anchor) -> None:
        self._service = service
        self._anchor = anchor

    @property
    def conversation_id(self) -> str:
        """Return the conversation's single identity: its anchor id."""
        return self._anchor.sub_agent_id or self._anchor.chat_id

    @property
    def workspace_id(self) -> str:
        """Return the workspace the conversation lives in."""
        return self._anchor.workspace_id

    @property
    def chat_id(self) -> str:
        """Return the chat that contains and stores the conversation."""
        return self._anchor.chat_id

    @property
    def kind(self) -> str:
        """Return the durable kind of the conversation's record, when present."""
        record = self._record()
        return "root" if record is None else record.kind.value

    # -- operations: each persists then wakes --------------------------------

    def deliver_session_message(
        self,
        message: SessionMessage,
        *,
        actor_id: str = "thesis-runtime",
        body: str | None = None,
        role: MessageRole | None = None,
        kind: TranscriptItemKind | None = None,
        data: Mapping[str, object] | None = None,
        tool_result: Mapping[str, object] | None = None,
        datums: Sequence[Datum] = (),
        execution_id: str | None = None,
        attachment_ids: tuple[str, ...] = (),
        references: Mapping[str, tuple[str, ...]] | None = None,
        intent_epoch: int = 0,
        idempotency_key: str | None = None,
        schedule: bool = True,
    ) -> ChatMessage:
        """Deliver at this handle's anchor through the canonical service port."""
        return self._service.deliver_session_message(
            self._anchor,
            message,
            actor_id=actor_id,
            body=body,
            role=role,
            kind=kind,
            data=data,
            tool_result=tool_result,
            datums=datums,
            execution_id=execution_id,
            attachment_ids=attachment_ids,
            references=references,
            intent_epoch=intent_epoch,
            idempotency_key=idempotency_key,
            schedule=schedule,
        )

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
        schedule: bool = True,
        message_id: str | None = None,
    ) -> ChatMessage:
        """Persist one message, optionally deferring a root wake until intake ends."""
        if self._anchor.sub_agent_id is not None:
            if not schedule:
                raise DeliveryError("only root input may defer scheduling")
            if role is not MessageRole.USER or attachment_ids or references:
                raise DeliveryError("nested input must be a plain user answer")
            return self._service.answer(
                self.workspace_id,
                self.chat_id,
                self._anchor.sub_agent_id,
                body=body,
                actor=AnsweringActor(actor_id),
                message_id=message_id,
            ).message
        return self.deliver_session_message(
            SessionMessage(
                message_id or f"message-{uuid.uuid4().hex}",
                "incoming",
                MessageContent(body, "user"),
            ),
            actor_id=actor_id,
            body=body,
            role=role,
            attachment_ids=tuple(attachment_ids),
            references=references,
            execution_id=execution_id,
            intent_epoch=intent_epoch,
            schedule=schedule,
        )

    def deliver(
        self,
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
        """Persist one datum at this anchor, then wake this conversation."""
        return self._service.deliver(
            self._anchor,
            datum,
            body=body,
            kind=kind,
            role=role,
            data=data,
            execution_id=execution_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            tool_result=tool_result,
            schedule=schedule,
        )

    def request_input(self) -> SubAgentRecord:
        """Open this conversation's input state so an answer may resume it.

        The pause is a lifecycle transition on the node's durable record: the
        question's state lives on the asking node, and the answer admitted
        through :meth:`Conversations.answer` consumes the input and returns the
        node to ``running``.
        """
        if self._anchor.sub_agent_id is None:
            raise NodeTurnError("a pause targets a nested conversation")
        return self._service.lifecycle.request_input(
            self._anchor.workspace_id, self._anchor.sub_agent_id
        )

    # -- observation ----------------------------------------------------------

    def status(self) -> str:
        """Return the conversation's durable status."""
        record = self._record()
        if record is None:
            thread = self._service.chat.get_chat(
                self._anchor.workspace_id, self._anchor.chat_id
            )
            return thread.state.value
        return record.status.value

    def in_flight(self) -> bool:
        """Return whether a turn is currently running for this conversation."""
        return self._service.in_flight(
            self._anchor.workspace_id,
            self._anchor.chat_id,
            self._anchor.sub_agent_id or ROOT_ANCHOR_ID,
        )

    def transcript(self) -> tuple[ChatMessage, ...]:
        """Return this conversation's own anchored messages, in order."""
        return self._service.transcript(self._anchor)

    # -- turn execution ------------------------------------------------------

    def run_turn(self) -> ChatMessage | SessionResult | None:
        """Run this conversation's next turn and return its outcome.

        The worker calls this; it may also be called directly to run a turn
        synchronously.  The root and every nested conversation run the same
        durable session turn over their own anchored transcript.
        """
        anchor = self._anchor.sub_agent_id or "root"
        _LOGGER.info("turn.start anchor=%s", anchor)
        started = time.monotonic()
        try:
            result = self._service.run_node_turn(
                self._anchor.workspace_id,
                self._anchor.chat_id,
                self._anchor.sub_agent_id or ROOT_ANCHOR_ID,
            )
            if not self._anchor.is_root:
                return result
            if result.status != "completed":
                return None
            if not result.turns or result.turns[-1].message.direction != "outgoing":
                return None
            final_invocation_id = result.turns[-1].invocation_id
            return next(
                (
                    message
                    for message in reversed(self.transcript())
                    if message.role is MessageRole.ASSISTANT
                    and isinstance(decode_activity(message), ModelOutput)
                    and getattr(decode_activity(message), "invocation_id", None)
                    == final_invocation_id
                ),
                None,
            )
        finally:
            _LOGGER.info(
                "turn.end anchor=%s duration_ms=%d",
                anchor,
                int((time.monotonic() - started) * 1000),
            )

    def _record(self) -> SubAgentRecord | None:
        if self._anchor.sub_agent_id is None:
            return None
        return self._service.store.get_sub_agent(
            self._anchor.workspace_id, self._anchor.sub_agent_id
        )
