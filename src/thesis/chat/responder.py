"""The chat reply chunk vocabulary and the development outcome summary.

The reply service itself no longer exists: a conversation at the root anchor
owns its reply turn, and the turn consumes the chunk stream defined here.
Answer text streams as ``str`` or :class:`ReplyChunk` (with
``kind="reasoning"`` for model chain-of-thought, surfaced on the separate
reasoning channel and persisted on the assistant message's ``reasoning``
field, never inside its ``body``); a tool outcome is yielded as a
:class:`ToolResultChunk` and persisted as a ``tool``-role message at its
position in the conversation.  The model call itself is injected as a
:data:`ReplyProvider` callable, so transport and credentials stay at the
composition boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from thesis.chat.contracts import ChatMessage

if TYPE_CHECKING:
    from thesis.workflows.registry.hypothesis_development import (
        HypothesisDevelopmentOutcome,
        HypothesisDevelopmentRequest,
    )


@dataclass(frozen=True, slots=True)
class ReplyChunk:
    """One streamed reply fragment tagged with its channel.

    ``kind`` is ``"reasoning"`` for model chain-of-thought and ``"text"`` (the
    default) for answer text.  A bare ``str`` from a provider is treated as
    answer text. ``"assistant_turn"`` is a complete intermediate assistant
    envelope: persist and publish it with ``data`` before requesting the next
    chunk, never append it to final answer text. ``"runtime_notification"`` is
    a runtime notice, also persisted separately from the answer. Closing the
    iterator cancels further model/tool dispatch cooperatively.
    """

    text: str
    kind: str = "text"
    data: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResultChunk:
    """One ephemeral tool outcome emitted alongside a streamed reply.

    A tool result is not answer text: it is persisted as its own ordered
    ``tool``-role chat message carrying the structured outcome, so the
    conversation retains the call in the exact position it ran.  ``call_id``
    ties the result to the model's ``tool_calls`` entry when the provider
    supplied one, and ``failure`` carries the canonical failure mapping for an
    unsuccessful call. ``data`` carries optional invocation metadata to merge
    into the persisted chat message's data, not its structured tool outcome.
    """

    tool: str
    call_id: str | None
    ok: bool
    summary: str
    structured: Mapping[str, object] = field(default_factory=dict)
    text: str = ""
    failure: Mapping[str, object] | None = None
    datum_type: str | None = None
    data: Mapping[str, object] | None = None


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


class DevelopmentRuntime(Protocol):
    """The narrow runtime surface for the hypothesis-development pipeline."""

    def build_hypothesis_development(
        self, *, chat: object
    ) -> LaunchableDevelopmentWorkflow:
        """Compose the hypothesis-development workflow over the runtime ports."""


class LaunchableDevelopmentWorkflow(Protocol):
    """The launch surface of the hypothesis-development workflow."""

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        ideas: str,
        launch_options: Mapping[str, object] | None = None,
        investigation_id: str | None = None,
        trigger_message_id: str | None = None,
    ) -> HypothesisDevelopmentRequest:
        """Persist the chat trigger and return the runnable request."""


#: Active (non-terminal) hypothesis-development phases, by wire value.
_ACTIVE_DEVELOPMENT_PHASES = frozenset(
    {
        "requested",
        "discovery",
        "model_creation",
        "falsification_trials",
        "rca",
        "verdict",
        "strategy_design",
        "optimization",
        "null_testing",
        "variation",
        "forward_validation",
    }
)


def development_summary(outcome: HypothesisDevelopmentOutcome) -> str:
    """Return the assistant summary for one hypothesis-development execution.

    A running execution is reported as started/running; only a genuine terminal
    failure is phrased as a failure, and a model-format compose problem is
    delivered as an operator request instead of ending the run.
    """
    phase = outcome.phase.value
    execution_id = outcome.execution_id
    if phase in _ACTIVE_DEVELOPMENT_PHASES:
        return (
            f"Hypothesis development {execution_id} started and is running ({phase})."
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


__all__ = [
    "DevelopmentRuntime",
    "LaunchableDevelopmentWorkflow",
    "ReplyChunk",
    "ReplyProvider",
    "ToolResultChunk",
    "ToolResultObserver",
    "development_summary",
]
