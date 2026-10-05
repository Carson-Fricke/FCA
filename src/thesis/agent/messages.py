"""Provider-neutral session values and the durable transcript decode boundary.

Directions describe session bookkeeping, not provider roles. Sources describe
content, not authority: callers must authenticate provenance out of band and
must never authorize tool execution from these values or their rendered text.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from types import MappingProxyType
from typing import ClassVar, Literal, Protocol, cast

from pydantic import ConfigDict, TypeAdapter

from thesis.chat.contracts import ChatMessage


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("result keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("result must contain only finite JSON values")


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _json_value(getattr(value, item.name)) for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class _ProviderRequest(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def arguments(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """One model-requested operation, without a provider wire representation."""

    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    id: str
    name: str
    arguments: str = ""

    def __post_init__(self) -> None:
        _text(self.id, "request id")
        _text(self.name, "request name")
        if not isinstance(self.arguments, str):
            raise ValueError("request arguments must be text")

    @classmethod
    def from_provider(cls, request: _ProviderRequest) -> ToolRequest:
        """Copy a completion request through its structural, neutral fields."""
        return cls(request.id, request.name, request.arguments)

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_value(self))


@dataclass(frozen=True, slots=True)
class ToolCorrelation:
    """The immutable, exact join to a request in a particular message."""

    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    request_message_id: str
    request_id: str
    tool_name: str

    def __post_init__(self) -> None:
        _text(self.request_message_id, "request message id")
        _text(self.request_id, "request id")
        _text(self.tool_name, "tool name")

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_value(self))


@dataclass(frozen=True, slots=True)
class MessageContent:
    """Uniform content for operator input, deliveries, prompts, and results."""

    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    text: str
    source: str
    correlation: ToolCorrelation | None = None
    result: Mapping[str, object] | None = None
    requests: tuple[ToolRequest, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("content text must be text")
        _text(self.source, "content source")
        if self.correlation is not None and not isinstance(
            self.correlation, ToolCorrelation
        ):
            raise ValueError("correlation must be a ToolCorrelation")
        if self.result is not None:
            if not isinstance(self.result, Mapping):
                raise ValueError("result must be a mapping")
            object.__setattr__(self, "result", _freeze(self.result))
        requests = tuple(self.requests)
        if any(not isinstance(request, ToolRequest) for request in requests):
            raise ValueError("requests must contain ToolRequest values")
        object.__setattr__(self, "requests", requests)

    def as_dict(self) -> dict[str, object]:
        """Return detached neutral fields for persistence."""
        return cast(dict[str, object], _json_value(self))

    def as_content(self) -> str:
        """Render incoming evidence once as JSON, without transport authority."""
        value = self.as_dict()
        value.pop("requests")
        return _json(value)


@dataclass(frozen=True, slots=True)
class SessionMessage:
    """An immutable message in the caller-supplied session scope and order."""

    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    message_id: str
    direction: Literal["incoming", "outgoing", "instruction"]
    content: MessageContent
    position: int = 0

    def __post_init__(self) -> None:
        _text(self.message_id, "message id")
        if self.direction not in ("incoming", "outgoing", "instruction"):
            raise ValueError("unsupported session direction")
        if not isinstance(self.content, MessageContent):
            raise ValueError("content must be MessageContent")
        if (
            not isinstance(self.position, int)
            or isinstance(self.position, bool)
            or self.position < 0
        ):
            raise ValueError("position must be a non-negative integer")
        if self.content.requests and self.direction != "outgoing":
            raise ValueError("only outgoing messages may contain requests")
        if self.content.correlation is not None and self.direction != "incoming":
            raise ValueError("only incoming messages may contain correlations")

    def as_dict(self) -> dict[str, object]:
        """Return the canonical value stored under ``data['sessionMessage']``."""
        return cast(dict[str, object], _json_value(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SessionMessage:
        """Decode exactly the canonical dataclass fields, rejecting extras."""
        return SESSION_MESSAGE_ADAPTER.validate_python(value)


SESSION_MESSAGE_ADAPTER = TypeAdapter(SessionMessage)


def from_record(record: ChatMessage) -> SessionMessage:
    """Decode one durable record through its canonical session message.

    Every persisted message carries ``data['sessionMessage']``. The durable
    identity must agree, so correlation joins cannot silently change envelopes,
    and the durable record's assigned position supersedes the pre-persistence
    value. No prose is parsed and no runtime module is imported here.
    """
    data = record.data or {}
    value = data.get("sessionMessage")
    if not isinstance(value, Mapping):
        raise ValueError("durable record has no canonical sessionMessage")
    message = SessionMessage.from_dict(value)
    if message.message_id != record.message_id:
        raise ValueError("sessionMessage identity differs from durable record")
    return replace(message, position=record.input_position)


def decode_transcript(records: Sequence[ChatMessage]) -> tuple[SessionMessage, ...]:
    """Decode the canonical session messages of these records, in order.

    Runtime activity/progress records without a canonical session message are
    presentation only: they take no part in provider projection or replay.
    """
    return tuple(
        from_record(record)
        for record in records
        if isinstance((record.data or {}).get("sessionMessage"), Mapping)
    )


__all__ = [
    "MessageContent",
    "SESSION_MESSAGE_ADAPTER",
    "SessionMessage",
    "ToolCorrelation",
    "ToolRequest",
    "decode_transcript",
    "from_record",
]
