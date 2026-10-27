"""Lossless reasoning fields and their separate, readable display projection.

Only provider decoders populate these fields. Stream indices assemble blocks;
they are transport coordinates, not part of the replayed continuation payload.
This module owns neither messages nor persistence or execution policy.
"""

from collections.abc import Mapping
from math import isfinite
from types import MappingProxyType

from thesis_runtime.contracts.errors import ProviderRejectedError

REASONING_FIELDS = ("reasoning", "reasoning_content", "reasoning_details")


def _copy(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ProviderRejectedError("provider continuation keys must be strings")
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_copy(item) for item in value]
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and isfinite(value):
        return value
    raise ProviderRejectedError("provider continuation must contain finite JSON values")


def freeze_continuation(fields: Mapping[str, object]) -> Mapping[str, object]:
    """Detach and freeze provider data before it enters an immutable message."""

    def freeze(value: object) -> object:
        if isinstance(value, Mapping):
            return MappingProxyType({key: freeze(item) for key, item in value.items()})
        if isinstance(value, list | tuple):
            return tuple(freeze(item) for item in value)
        return value

    return MappingProxyType(
        {name: freeze(value) for name, value in reasoning_fields(fields).items()}
    )


def reasoning_fields(message: Mapping[str, object]) -> dict[str, object]:
    """Copy documented continuation fields, preserving absence and empty values."""
    result = {}
    for name in REASONING_FIELDS:
        if name not in message:
            continue
        value = message[name]
        if value is None:
            continue
        if name == "reasoning_details":
            if not isinstance(value, list | tuple) or any(
                not isinstance(block, Mapping) for block in value
            ):
                raise ProviderRejectedError(
                    "provider emitted malformed reasoning details"
                )
        elif not isinstance(value, str):
            raise ProviderRejectedError("provider emitted non-text reasoning")
        result[name] = _copy(value)
    return result


def reasoning_text(fields: Mapping[str, object]) -> str:
    """Derive display text without modifying the exact replay fields."""
    for name in ("reasoning", "reasoning_content"):
        value = fields.get(name)
        if isinstance(value, str) and value:
            return value
    details = fields.get("reasoning_details", ())
    if not isinstance(details, list | tuple):
        return ""
    parts = []
    for block in details:
        if not isinstance(block, Mapping):
            continue
        field = {"reasoning.text": "text", "reasoning.summary": "summary"}.get(
            str(block.get("type"))
        )
        value = block.get(field) if field is not None else None
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def _merge_readable(
    previous: Mapping[str, object] | None, current: Mapping[str, object]
) -> dict[str, object] | None:
    """Merge consecutive compatible text; encrypted/unknown blocks stay opaque."""
    if previous is None or previous.get("type") != current.get("type"):
        return None
    field = {"reasoning.text": "text", "reasoning.summary": "summary"}.get(
        str(current.get("type"))
    )
    if field is None:
        return None
    for key in ("id", "index", "format"):
        before, after = previous.get(key), current.get(key)
        if before is not None and after is not None and before != after:
            return None
    if (
        previous.get("signature")
        and current.get("signature")
        and previous["signature"] != current["signature"]
    ):
        return None
    before, after = previous.get(field, ""), current.get(field, "")
    if not isinstance(before, str) or not isinstance(after, str):
        raise ProviderRejectedError("provider emitted a non-text reasoning fragment")
    merged = {**previous, **current, field: before + after}
    for key in ("signature", "format"):
        if key in previous or key in current:
            merged[key] = previous.get(key) or current.get(key)
    return merged


class ReasoningStream:
    """Accumulate text deltas while keeping opaque reasoning blocks separate."""

    def __init__(self) -> None:
        self._scalar_name: str | None = None
        self._scalar_parts: list[str] = []
        self._blocks: list[dict[str, object]] = []
        self._has_details = False

    def add(self, delta: Mapping[str, object]) -> None:
        fields = reasoning_fields(delta)
        self._has_details = self._has_details or "reasoning_details" in fields
        # These are aliases for one channel. A gateway can change the alias
        # between frames; concatenate the text once under the first observed name.
        names = [name for name in ("reasoning", "reasoning_content") if name in fields]
        if names:
            name = next((name for name in names if fields[name]), names[0])
            if self._scalar_name is None:
                self._scalar_name = name
            self._scalar_parts.append(str(fields[name]))
        details = fields.get("reasoning_details", ())
        assert isinstance(details, list | tuple)
        for block in details:
            assert isinstance(block, Mapping)
            index = block.get("index")
            if index is not None and (type(index) is not int or index < 0):
                raise ProviderRejectedError(
                    "provider reasoning block requires a valid index"
                )
            current = dict(block)
            previous = self._blocks[-1] if self._blocks else None
            merged = _merge_readable(previous, current)
            if merged is None:
                self._blocks.append(current)
            else:
                self._blocks[-1] = merged

    def fields(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self._scalar_name is not None:
            result[self._scalar_name] = "".join(self._scalar_parts)
        if self._has_details:
            result["reasoning_details"] = [
                {name: _copy(value) for name, value in block.items() if name != "index"}
                for block in self._blocks
            ]
        return result
