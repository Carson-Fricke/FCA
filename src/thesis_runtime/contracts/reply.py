"""Neutral tool-result observation contracts; model output lives in output.py."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


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


__all__ = ["ToolResultChunk", "ToolResultObserver"]
