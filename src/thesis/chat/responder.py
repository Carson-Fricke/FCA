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

import json
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

#: The explicit structured trigger that requests a hypothesis-development run.
_DEVELOPMENT_WORKFLOW_TRIGGER = "hypothesis-development"


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


def launch_hypothesis_development(
    runtime: DevelopmentRuntime,
    chat: ChatService,
    *,
    workspace_id: str,
    chat_id: str,
    actor_id: str,
    ideas: str,
    investigation_id: str | None = None,
    trigger_message_id: str | None = None,
) -> tuple[HypothesisDevelopmentRequest, HypothesisDevelopmentOutcome]:
    """Launch and fully execute one falsification-first hypothesis pipeline.

    The launch goes through the composed workflow (and therefore through
    :meth:`~thesis.chat.service.ChatService.launch_workflow`), and the run is
    executed through the same injected runtime.  Callers own error handling: a
    misconfigured runtime or an unhandled provider failure propagates.
    """
    workflow = runtime.build_hypothesis_development(chat=chat)
    request = workflow.launch(
        workspace_id,
        chat_id,
        actor_id=actor_id,
        ideas=ideas,
        investigation_id=investigation_id,
        trigger_message_id=trigger_message_id,
    )
    outcome = runtime.execute_hypothesis_development(request, chat=chat)
    return request, outcome


def _development_trigger(message: ChatMessage) -> str | None:
    """Return the ideas when a message is an explicit development trigger.

    Only a user message carrying the exact structured JSON envelope
    ``{"workflow": "hypothesis-development", "ideas": "<ideas>"}`` triggers the
    falsification-first pipeline.  There is no natural-language inference.
    """
    if message.role is not MessageRole.USER:
        return None
    text = message.body.strip()
    if not text.startswith("{"):
        return None
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    if document.get("workflow") != _DEVELOPMENT_WORKFLOW_TRIGGER:
        return None
    ideas = document.get("ideas")
    if not isinstance(ideas, str) or not ideas.strip():
        return None
    return ideas.strip()


#: Phrasings that explicitly frame a message as an idea to research.
_RESEARCH_IDEA_FRAMING = (
    "i have an idea",
    "i've got an idea",
    "i have a theory",
    "i've got a theory",
    "my theory",
    "my hypothesis",
    "a hypothesis",
    "the hypothesis",
    "here's an idea",
    "here is an idea",
    "here's a theory",
    "here is a theory",
    "trade idea",
    "trading idea",
    "strategy idea",
    "research idea",
    "market idea",
    "investment idea",
    "an edge",
    "the edge",
    "what if",
    "let's research",
    "lets research",
    "research the",
    "explore the",
    "investigate the",
    "test the hypothesis",
    "test the theory",
    "test the idea",
    "develop a hypothesis",
    "develop a strategy",
    "build a hypothesis",
    "falsify",
    "falsification",
)

#: Imperative openers that request research directly.
_RESEARCH_IDEA_OPENERS = (
    "research ",
    "explore ",
    "investigate ",
    "falsify ",
    "validate ",
    "backtest ",
)

#: Causal or predictive cues describing a market mechanism worth testing.
_CAUSAL_CLAIM_CUES = (
    "predicts",
    "predict",
    "because",
    "due to",
    "leads to",
    "lead to",
    "drives",
    "driven by",
    "causes",
    "caused by",
    "underreact",
    "overreact",
    "mispric",
    "mean reversion",
    "mean-revert",
    "tends to",
    "tend to",
    "correlat",
    "outperform",
    "underperform",
    "alpha",
    "has an edge",
    "have an edge",
)

#: Words that anchor a claim to a tradable market subject.
_MARKET_SUBJECTS = (
    "market",
    "stock",
    "equit",
    "price",
    "return",
    "volatil",
    "vix",
    "spy",
    "qqq",
    "option",
    "future",
    "bond",
    "yield",
    "rate",
    "momentum",
    "trend",
    "sector",
    "earnings",
    "spread",
    "commodit",
    "currency",
    "dollar",
    "index",
    "ticker",
    "asset",
)

_INTERROGATIVE_PREFIXES = (
    "what",
    "why",
    "how",
    "when",
    "where",
    "who",
    "which",
    "is",
    "are",
    "was",
    "were",
    "does",
    "do",
    "did",
    "can",
    "could",
    "should",
    "would",
    "will",
    "may",
    "might",
)


def _looks_like_question(lowered: str) -> bool:
    """Return whether a lower-cased message reads as an ordinary question."""
    stripped = lowered.strip()
    if stripped.endswith("?"):
        return True
    first = stripped.split(maxsplit=1)[0] if stripped else ""
    return first in _INTERROGATIVE_PREFIXES


def _research_ideas(message: ChatMessage) -> str | None:
    """Return the ideas when a plain-language message asks to research one.

    This is a conservative, deterministic predicate: it launches the
    falsification-first pipeline only for messages that clearly frame a
    tradable idea, theory, edge, or hypothesis.  Ordinary questions, greetings,
    and status requests never match and continue to the model reply provider.
    """
    if message.role is not MessageRole.USER:
        return None
    text = message.body.strip()
    if not text:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in _RESEARCH_IDEA_FRAMING):
        return text
    if lowered.startswith(_RESEARCH_IDEA_OPENERS):
        return text
    if _looks_like_question(lowered):
        return None
    if any(cue in lowered for cue in _CAUSAL_CLAIM_CUES) and any(
        subject in lowered for subject in _MARKET_SUBJECTS
    ):
        return text
    return None


def _development_summary(outcome: HypothesisDevelopmentOutcome) -> str:
    """Return the assistant summary for one finished hypothesis pipeline."""
    verdict = outcome.verdict
    if verdict is None:
        return (
            f"Hypothesis development {outcome.execution_id} finished "
            f"({outcome.phase.value}); no falsification verdict was recorded."
        )
    return (
        f"Hypothesis development {outcome.execution_id} finished "
        f"({outcome.phase.value}); falsification verdict: {verdict.outcome}. "
        f"{verdict.reason}"
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
    return {
        "callId": chunk.call_id,
        "failure": None if chunk.failure is None else dict(chunk.failure),
        "ok": chunk.ok,
        "structured": dict(chunk.structured),
        "summary": chunk.summary,
        "text": chunk.text,
        "tool": chunk.tool,
    }


@dataclass(slots=True)
class ChatReplyService:
    """Generate and stream one assistant reply per user message.

    A user message carrying the explicit structured development trigger, or a
    plain-language research idea, is executed as a falsification-first
    hypothesis-development run instead of a model reply; the finished verdict
    is persisted as an assistant message and the existing execution events are
    emitted by the launched workflow.  Without a development runtime the trigger
    is acknowledged with an error delta and nothing is persisted, so the surface
    fails closed.  Every other message goes to the ordinary reply provider, so
    normal Q&A is never hijacked.
    """

    chat_service: ChatService
    broker: InMemoryChatStreamBroker
    provider: ReplyProvider
    actor_id: str = "thesis-agent"
    max_history: int = 40
    id_factory: Callable[[], str] = field(default=_stream_id)
    development_runtime: DevelopmentRuntime | None = None
    tool_result_observer: ToolResultObserver | None = None

    def respond(self, workspace_id: str, chat_id: str) -> ChatMessage | None:
        """Stream and persist the assistant reply; return the durable message.

        Returns ``None`` when there is no conversation, no reply text, or an
        unhandled launch/provider failure; failures are published as error
        deltas and never raise into the background task boundary.
        """
        history = tuple(
            message
            for message in self.chat_service.list_messages(workspace_id, chat_id)
            if message.role in _HISTORY_ROLES
        )[-self.max_history :]
        if not history:
            return None
        trigger_ideas = _development_trigger(history[-1])
        if trigger_ideas is None:
            trigger_ideas = _research_ideas(history[-1])
        if trigger_ideas is not None:
            return self._respond_with_development(
                workspace_id,
                chat_id,
                trigger_ideas,
                trigger_message_id=history[-1].message_id,
            )
        return self._respond_with_provider(workspace_id, chat_id, history)

    def _respond_with_development(
        self,
        workspace_id: str,
        chat_id: str,
        ideas: str,
        *,
        trigger_message_id: str | None = None,
    ) -> ChatMessage | None:
        """Launch and execute the hypothesis pipeline, then persist its verdict."""
        stream_id = self.id_factory()
        self._start_delta(workspace_id, chat_id, stream_id)
        runtime = self.development_runtime
        if runtime is None:
            self._error_delta(
                workspace_id,
                chat_id,
                stream_id,
                "workflow runtime is not configured",
            )
            return None
        try:
            _, outcome = launch_hypothesis_development(
                runtime,
                self.chat_service,
                workspace_id=workspace_id,
                chat_id=chat_id,
                actor_id=self.actor_id,
                ideas=ideas,
                trigger_message_id=trigger_message_id,
            )
            body = (
                _sanitize(_development_summary(outcome))
                or "Hypothesis development finished."
            )
            message = self.chat_service.create_message(
                workspace_id,
                chat_id,
                actor_id=self.actor_id,
                body=body,
                role=MessageRole.ASSISTANT,
                execution_id=outcome.execution_id,
            )
        except Exception as error:  # fail closed at the task boundary
            self._error_delta(workspace_id, chat_id, stream_id, str(error))
            return None
        self.broker.publish(
            workspace_id, chat_id, CHAT_MESSAGE, {"message": message.as_dict()}
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

    def _respond_with_provider(
        self, workspace_id: str, chat_id: str, history: Sequence[ChatMessage]
    ) -> ChatMessage | None:
        """Stream and persist an ordinary model reply."""
        stream_id = self.id_factory()
        self._start_delta(workspace_id, chat_id, stream_id)
        answer_chunks: list[str] = []
        reasoning_chunks: list[str] = []
        try:
            for chunk in self.provider(history):
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
    "launch_hypothesis_development",
]
