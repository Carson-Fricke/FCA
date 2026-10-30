"""Lossless, model-visible failures shared by tool adapters and dispatch."""

from collections.abc import Iterator, Mapping
from types import MappingProxyType


class ToolFailure(Mapping[str, object]):
    """A failed boundary must retain a nonempty category and explanation."""

    def __init__(self, value: Mapping[str, object]) -> None:
        for field in ("kind", "message"):
            if not isinstance(value.get(field), str) or not str(value[field]).strip():
                raise ValueError(f"tool failure requires nonempty {field}")
        self._value = MappingProxyType(dict(value))

    def __getitem__(self, key: str) -> object:
        return self._value[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return repr(dict(self._value))


class ToolFailureError(ValueError):
    """Carry domain evidence through exception boundaries without stringifying it."""

    def __init__(self, failure: Mapping[str, object]) -> None:
        self.failure = ToolFailure(failure)
        super().__init__(str(self.failure["message"]))


def failure_from_exception(error: Exception, **context: object) -> ToolFailure:
    """Preserve structured evidence; context supplies only missing boundary facts."""
    retained = getattr(error, "failure", None)
    payload = getattr(error, "payload", None)
    value = dict(context)
    if isinstance(payload, Mapping):
        value.update(payload)
    if isinstance(retained, Mapping):
        value.update(retained)
    value.setdefault("kind", "tool")
    value.setdefault("message", str(error) or type(error).__name__)
    certainty = getattr(error, "execution_certainty", None)
    if certainty is None and isinstance(retained, Mapping):
        certainty = retained.get("certainty", retained.get("executionCertainty"))
    if certainty is not None:
        value["certainty"] = certainty
        value["executionCertainty"] = certainty
    return ToolFailure(value)
