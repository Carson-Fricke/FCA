"""Provider-agnostic chat reply generation with streamed deltas.

The reply service is the missing link between the chat-owned message surface
and the model providers: on a new user message it assembles the conversation,
streams the model's reply as ``provider.delta`` frames, persists one assistant
``ChatMessage``, and publishes a final ``chat.message`` frame.  Model
chain-of-thought is a separate, explicitly-surfaced channel: reasoning chunks
stream as ``provider.delta`` frames with ``kind: "reasoning"`` before the
answer text and are persisted on the assistant message's ``reasoning`` field,
never inside its ``body``.  The model call itself is injected as a
:data:`ReplyProvider` callable, so the transport and credentials stay at the
composition boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from thesis.chat.contracts import ChatMessage, MessageRole, TranscriptItemKind
from thesis.chat.service import ChatService
from thesis.chat.streaming import CHAT_MESSAGE, PROVIDER_DELTA, InMemoryChatStreamBroker

if TYPE_CHECKING:
    from thesis.workflows.hypothesis_development import (
        HypothesisDevelopmentOutcome,
        HypothesisDevelopmentRequest,
    )


@dataclass(frozen=True, slots=True)
class ReplyChunk:
    """One streamed reply fragment tagged with its channel.

    ``kind`` is ``"reasoning"`` for model chain-of-thought and ``"text"`` (the
    default) for answer text.  A bare ``str`` from a provider is treated as
    answer text.
    """

    text: str
    kind: str = "text"


@dataclass(frozen=True, slots=True)
class ToolResultChunk:
    """One ephemeral tool outcome emitted alongside a streamed reply.

    A tool result is not answer text: it is persisted as its own ordered
    ``tool``-role chat message carrying the structured outcome, so the
    conversation retains the call in the exact position it ran.  ``call_id``
    ties the result to the model's ``tool_calls`` entry when the provider
    supplied one, and ``failure`` carries the canonical failure mapping for an
    unsuccessful call.
    """

    tool: str
    call_id: str | None
    ok: bool
    summary: str
    structured: Mapping[str, object] = field(default_factory=dict)
    text: str = ""
    failure: Mapping[str, object] | None = None
    datum_type: str | None = None


#: Observes one tool result before it is persisted and may enrich it.
#:
#: The observer receives the originating ``workspace_id``, ``chat_id``, and
#: optional ``execution_id`` plus the :class:`ToolResultChunk`; returning a
#: replacement chunk merges its data into the persisted tool message.
ToolResultObserver = Callable[[str, str, str | None, ToolResultChunk], ToolResultChunk]


#: A reply provider maps a conversation to an iterator of reply chunks.
#:
#: Answer text may be yielded either as a bare ``str`` or as a
#: :class:`ReplyChunk` with ``kind="text"``.  Chain-of-thought is yielded as a
#: :class:`ReplyChunk` with ``kind="reasoning"`` and is always surfaced on the
#: separate reasoning channel, never mixed into the answer.  A tool outcome is
#: yielded as a :class:`ToolResultChunk` and persisted as a ``tool``-role
#: message at its position in the conversation.
ReplyProvider = Callable[
    [Sequence[ChatMessage]], Iterator[str | ReplyChunk | ToolResultChunk]
]

_HISTORY_ROLES = frozenset(
    (MessageRole.USER, MessageRole.ASSISTANT, MessageRole.SYSTEM)
)


def _owns_anchor(message: ChatMessage, sub_agent_id: str | None) -> bool:
    """Return whether ``message`` belongs to the conversation at the anchor.

    The root conversation (``sub_agent_id is None``) owns every message with no
    sub-agent anchor; a node owns only its own.  A message created without a
    ``data`` payload is root-owned.  Seeding is parameterized by anchor so a
    node's turns never leak into another conversation's prompt.
    """
    anchor = None if message.data is None else message.data.get("subAgentId")
    return anchor == sub_agent_id


class DevelopmentRuntime(Protocol):
    """The narrow runtime surface for the hypothesis-development pipeline."""

    def build_hypothesis_development(
        self, *, chat: ChatService
    ) -> LaunchableDevelopmentWorkflow:
        """Compose the hypothesis-development workflow over the runtime ports."""

    def execute_hypothesis_development(
        self,
        request: HypothesisDevelopmentRequest,
        *,
        chat: ChatService,
    ) -> HypothesisDevelopmentOutcome:
        """Run one launched hypothesis-development request to completion."""


class LaunchableDevelopmentWorkflow(Protocol):
    """The launch surface of the hypothesis-development workflow."""

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        ideas: str,
        investigation_id: str | None = None,
        trigger_message_id: str | None = None,
    ) -> HypothesisDevelopmentRequest:
        """Persist the chat trigger and return the runnable request."""


#: Active (non-terminal) hypothesis-development phases, by wire value.
_ACTIVE_DEVELOPMENT_PHASES = frozenset(
    {
        "requested",
        "hypothesis_creation",
        "falsification_trials",
        "rca",
        "verdict",
        "strategy_design",
        "optimization",
    }
)


def development_summary(outcome: HypothesisDevelopmentOutcome) -> str:
    """Return the assistant summary for one hypothesis-development execution.

    A running or paused execution is reported as started/running or
    needs-input; only a genuine terminal failure is phrased as a failure, and a
    model-format compose problem is never described as "finished (failed)"
    because it pauses the workflow instead.
    """
    phase = outcome.phase.value
    execution_id = outcome.execution_id
    if phase in _ACTIVE_DEVELOPMENT_PHASES:
        return (
            f"Hypothesis development {execution_id} started and is running ({phase})."
        )
    if phase == "paused":
        question = (outcome.detail or "").strip() or "operator input is required"
        return (
            f"Hypothesis development {execution_id} is paused and needs input: "
            f"{question} Answer the workflow's needs-input request to continue."
        )
    if phase == "failed":
        reason = (outcome.detail or "").strip() or "an infrastructure error"
        return f"Hypothesis development {execution_id} failed: {reason}."
    verdict = outcome.verdict
    if verdict is None:
        return (
            f"Hypothesis development {execution_id} finished ({phase}); "
            "no falsification verdict was recorded."
        )
    return (
        f"Hypothesis development {execution_id} finished ({phase}); "
        f"falsification verdict: {verdict.outcome}. {verdict.reason}"
    )


def _stream_id() -> str:
    return f"stream-{uuid.uuid4().hex}"


def _sanitize(text: str) -> str:
    """Keep printable characters plus newlines/tabs; drop other control chars."""
    return "".join(
        character for character in text if ord(character) >= 32 or character in "\n\t"
    ).strip()


def _tool_result_data(chunk: ToolResultChunk) -> dict[str, object]:
    """Return the canonical UI payload for one streamed tool outcome."""
    payload: dict[str, object] = {
        "callId": chunk.call_id,
        "failure": None if chunk.failure is None else dict(chunk.failure),
        "ok": chunk.ok,
        "structured": dict(chunk.structured),
        "summary": chunk.summary,
        "text": chunk.text,
        "tool": chunk.tool,
    }
    if chunk.datum_type is not None:
        payload["datumType"] = chunk.datum_type
    return payload


@dataclass(slots=True)
class ChatReplyService:
    """Generate and stream one assistant reply per user message.

    Every user message goes to the injected :data:`ReplyProvider`; a workflow is
    started only through the ``start_workflow`` tool the provider exposes, never
    by a chat-message protocol.  The provider streams text, reasoning, and tool
    outcomes, and this service persists the assistant message and each tool
    result in the order they ran.  The optional ``development_runtime`` remains
    available to the composition for building the workflow-launch tool.
    """

    chat_service: ChatService
    broker: InMemoryChatStreamBroker
    provider: ReplyProvider
    provider_factory: (
        Callable[[str, str, Sequence[ChatMessage]], ReplyProvider] | None
    ) = None
    actor_id: str = "thesis-agent"
    max_history: int = 40
    id_factory: Callable[[], str] = field(default=_stream_id)
    development_runtime: DevelopmentRuntime | None = None
    tool_result_observer: ToolResultObserver | None = None

    def respond(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        sub_agent_id: str | None = None,
    ) -> ChatMessage | None:
        """Stream and persist the assistant reply; return the durable message.

        The prompt history is the conversation at ``sub_agent_id``: the root
        chat by default, or one node's own turns.  Another anchor's turns are
        never seeded into this conversation.

        Returns ``None`` when there is no conversation, no reply text, or an
        unhandled launch/provider failure; failures are published as error
        deltas and never raise into the background task boundary.
        """
        history = tuple(
            message
            for message in self.chat_service.list_messages(workspace_id, chat_id)
            if message.role in _HISTORY_ROLES and _owns_anchor(message, sub_agent_id)
        )[-self.max_history :]
        if not history:
            return None
        return self._respond_with_provider(workspace_id, chat_id, history)

    def _respond_with_provider(
        self, workspace_id: str, chat_id: str, history: Sequence[ChatMessage]
    ) -> ChatMessage | None:
        """Stream and persist an ordinary model reply."""
        stream_id = self.id_factory()
        self._start_delta(workspace_id, chat_id, stream_id)
        answer_chunks: list[str] = []
        reasoning_chunks: list[str] = []
        provider = (
            self.provider
            if self.provider_factory is None
            else self.provider_factory(workspace_id, chat_id, history)
        )
        try:
            for chunk in provider(history):
                if isinstance(chunk, ToolResultChunk):
                    chunk = self._observe_tool_result(workspace_id, chat_id, chunk)
                    tool_message = self.chat_service.create_message(
                        workspace_id,
                        chat_id,
                        actor_id=self.actor_id,
                        body=chunk.summary or f"{chunk.tool} result",
                        role=MessageRole.TOOL,
                        tool_result=_tool_result_data(chunk),
                        kind=TranscriptItemKind.TOOL,
                    )
                    self.broker.publish(
                        workspace_id,
                        chat_id,
                        CHAT_MESSAGE,
                        {"message": tool_message.as_dict()},
                    )
                    continue
                if isinstance(chunk, ReplyChunk):
                    kind = chunk.kind
                    text = chunk.text
                else:
                    kind = "text"
                    text = chunk
                if not text:
                    continue
                if kind == "reasoning":
                    reasoning_chunks.append(text)
                    self.broker.publish(
                        workspace_id,
                        chat_id,
                        PROVIDER_DELTA,
                        {
                            "messageId": stream_id,
                            "text": text,
                            "kind": "reasoning",
                        },
                    )
                    continue
                answer_chunks.append(text)
                self.broker.publish(
                    workspace_id,
                    chat_id,
                    PROVIDER_DELTA,
                    {"messageId": stream_id, "text": text},
                )
        except Exception as error:  # fail closed at the task boundary
            self._error_delta(workspace_id, chat_id, stream_id, str(error))
            return None
        body = _sanitize("".join(answer_chunks))
        reasoning = _sanitize("".join(reasoning_chunks))
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
                reasoning=reasoning or None,
            )
        except Exception as error:
            # A reply must never be dropped silently: surface the failure on the
            # stream instead of letting the persistence error escape.
            self._error_delta(workspace_id, chat_id, stream_id, str(error))
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

    def _observe_tool_result(
        self, workspace_id: str, chat_id: str, chunk: ToolResultChunk
    ) -> ToolResultChunk:
        """Run the optional observer, returning an enriched chunk or the original.

        An observer fault must never break the reply, so any exception or
        unexpected return value collapses to the unmodified chunk.
        """
        observer = self.tool_result_observer
        if observer is None:
            return chunk
        try:
            observed = observer(workspace_id, chat_id, None, chunk)
        except Exception:  # observer failures are non-fatal to the reply
            return chunk
        return observed if isinstance(observed, ToolResultChunk) else chunk

    def _start_delta(self, workspace_id: str, chat_id: str, stream_id: str) -> None:
        self.broker.publish(
            workspace_id,
            chat_id,
            PROVIDER_DELTA,
            {"messageId": stream_id, "role": "assistant", "text": "", "start": True},
        )

    def _error_delta(
        self, workspace_id: str, chat_id: str, stream_id: str, message: str
    ) -> None:
        self.broker.publish(
            workspace_id,
            chat_id,
            PROVIDER_DELTA,
            {"messageId": stream_id, "error": message, "done": True},
        )


__all__ = [
    "ChatReplyService",
    "DevelopmentRuntime",
    "LaunchableDevelopmentWorkflow",
    "ReplyChunk",
    "ReplyProvider",
    "ToolResultChunk",
    "ToolResultObserver",
    "development_summary",
]
