"""Typed datums: the contract between a tool result and the chat surface.

A tool returns data, not rendering instructions.  Each value it returns is a
:class:`Datum` carrying its declared type; the shared chat surface selects the
canonical rendering for that type and renders the datum at the caller's anchor.
One tool call may return several datums of different types, and those datums may
arrive asynchronously.

Harness tools keep their own tool name as the implicit datum type, so the
existing per-tool renderers stay valid while the surface migrates to type-keyed
rendering.  Thesis-owned datums use the well-known types below.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class DatumType(StrEnum):
    """Well-known datum types with a canonical surface rendering."""

    #: The generic fallback: a tool outcome with no richer rendering.
    TOOL_RESULT = "tool_result"
    #: A workflow execution handle or state change.
    WORKFLOW_EXECUTION = "workflow_execution"
    #: A workflow's terminal outcome: execution id, status, and final phase.
    WORKFLOW_OUTCOME = "workflow_outcome"
    #: A newly created sub-agent handle.
    SUB_AGENT_HANDLE = "sub_agent_handle"
    #: A sub-agent's current state or terminal result.
    SUB_AGENT_STATUS = "sub_agent_status"
    #: An operator question published by an agent.
    OPERATOR_QUESTION = "operator_question"
    #: The first prompt delivered to a newly spawned agent.
    AGENT_PROMPT = "agent_prompt"


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def tool_result_payload(
    *,
    call_id: str | None,
    ok: object,
    failure: object,
    structured: object,
    summary: object,
    text: object,
    tool: str | None,
    datum_type: str | None = None,
) -> dict[str, object]:
    """Return the canonical durable payload for one tool outcome.

    Both the chat reply path and the node turn runner build their tool-result
    messages through this function so the stored shape is identical on every
    anchor.
    """
    payload: dict[str, object] = {
        "callId": call_id,
        "failure": dict(failure) if isinstance(failure, Mapping) else None,
        "ok": ok,
        "structured": dict(structured) if isinstance(structured, Mapping) else {},
        "summary": summary,
        "text": text,
        "tool": tool,
    }
    if datum_type is not None:
        payload["datumType"] = datum_type
    return payload


@dataclass(frozen=True, slots=True)
class Datum:
    """One typed value returned by a tool."""

    datum_type: str
    ok: bool = True
    structured: Mapping[str, object] = field(default_factory=dict)
    text: str = ""
    summary: str | None = None
    failure: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.datum_type, str) or not self.datum_type.strip():
            raise ValueError("datum_type must be non-empty text")
        object.__setattr__(self, "structured", MappingProxyType(dict(self.structured)))
        if self.failure is not None:
            object.__setattr__(self, "failure", MappingProxyType(dict(self.failure)))

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, JSON-ready datum."""
        return {
            "datumType": self.datum_type,
            "failure": None if self.failure is None else dict(self.failure),
            "ok": self.ok,
            "structured": dict(self.structured),
            "summary": self.summary,
            "text": self.text,
        }


def datum_from_result(
    datum_type: str,
    *,
    ok: bool,
    structured: Mapping[str, object] | None = None,
    text: str = "",
    summary: str | None = None,
    failure: Mapping[str, object] | None = None,
) -> Datum:
    """Build one datum, defaulting its summary from its text."""
    return Datum(
        datum_type=datum_type,
        ok=ok,
        structured=structured or {},
        text=text,
        summary=summary,
        failure=failure,
    )


def normalize_datums(
    datums: Sequence[Datum],
    *,
    fallback_type: str,
    ok: bool,
    structured: Mapping[str, object] | None = None,
    text: str = "",
    failure: Mapping[str, object] | None = None,
) -> tuple[Datum, ...]:
    """Return the declared datums, or one fallback datum for the tool."""
    if datums:
        return tuple(datums)
    return (
        Datum(
            datum_type=_text(fallback_type) or DatumType.TOOL_RESULT.value,
            ok=ok,
            structured=structured or {},
            text=text,
            failure=failure,
        ),
    )


__all__ = [
    "Datum",
    "DatumType",
    "datum_from_result",
    "normalize_datums",
]
