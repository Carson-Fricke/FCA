"""Pure model-family tool presentation and completion decoding contracts."""

from collections.abc import Mapping, Sequence
from typing import Protocol

from thesis_runtime.adapters.providers.invocations import ToolCall


class ModelFamilyAdapter(Protocol):
    family: str
    version: str

    def prepare_tools(
        self, tools: Sequence[Mapping[str, object]]
    ) -> tuple[Mapping[str, object], ...]: ...

    def decode_calls(self, calls: Sequence[ToolCall]) -> tuple[ToolCall, ...]: ...


class NativeToolsAdapter:
    """Shared native tool behavior for families without a specialized adapter."""

    family = "generic"
    version = "1"

    def prepare_tools(
        self, tools: Sequence[Mapping[str, object]]
    ) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(tool) for tool in tools)

    def decode_calls(self, calls: Sequence[ToolCall]) -> tuple[ToolCall, ...]:
        # Preserve raw arguments; canonical dispatch owns validation and repair feedback.
        return tuple(calls)
