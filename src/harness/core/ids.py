"""Deterministic identifiers for the synchronous simulation engine.

The engine must not derive identity from time or process-global randomness.
``EngineIdSource`` is intentionally stateful only through its monotonic local
counter.  Live broker client IDs are outside this module and should be created
by the live adapter using its globally unique UUID7 policy.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID


@dataclass(slots=True)
class EngineIdSource:
    """Issue reproducible UUID strings from a required seed and counter."""

    seed: str | int | bytes
    _counter: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool):
            raise TypeError("ID seed must be a string, integer, or bytes")
        if isinstance(self.seed, bytes):
            if not self.seed:
                raise ValueError("ID seed must not be empty")
        elif isinstance(self.seed, (str, int)):
            if isinstance(self.seed, str) and not self.seed:
                raise ValueError("ID seed must not be empty")
        else:
            raise TypeError("ID seed must be a string, integer, or bytes")
        if self._counter < 0:
            raise ValueError("ID counter must not be negative")

    @property
    def issued(self) -> int:
        """Return the number of IDs issued by this source."""
        return self._counter

    def next_uuid(self, kind: str = "engine") -> UUID:
        """Issue the next deterministic UUID for ``kind``."""
        if not isinstance(kind, str):
            raise TypeError("ID kind must be a string")
        if not kind:
            raise ValueError("ID kind must be a non-empty string")
        ordinal = self._counter
        self._counter += 1
        seed = _seed_bytes(self.seed)
        digest = hashlib.sha256(
            seed + b"\0" + kind.encode("utf-8") + b"\0" + ordinal.to_bytes(16, "big")
        ).digest()[:16]
        # Mark the identifier as UUID version 5 while retaining deterministic
        # bytes.  No UUID constructor here consults a clock or random source.
        value = bytearray(digest)
        value[6] = (value[6] & 0x0F) | 0x50
        value[8] = (value[8] & 0x3F) | 0x80
        return UUID(bytes=bytes(value))

    def next_id(self, kind: str = "engine") -> str:
        """Issue the next deterministic identifier as a string."""
        return str(self.next_uuid(kind))

    def __call__(self, kind: str = "engine") -> str:
        """Use the source as a small ID factory."""
        return self.next_id(kind)

    def next_event_id(self) -> str:
        """Issue the next deterministic event identifier."""
        return self.next_id("event")

    def next_order_id(self) -> str:
        """Issue the next deterministic simulated order identifier."""
        return self.next_id("order")

    def next_fill_id(self) -> str:
        """Issue the next deterministic fill identifier."""
        return self.next_id("fill")

    def next_report_id(self) -> str:
        """Issue the next deterministic execution-report identifier."""
        return self.next_id("report")


DeterministicIdSource = EngineIdSource
SeededIdSource = EngineIdSource
EngineIdFactory = EngineIdSource


def deterministic_id(seed: str | int | bytes, kind: str, ordinal: int) -> str:
    """Return an ID without mutating a source.

    This helper is useful when rebuilding an event from a persisted ordinal.
    It uses the same derivation as :class:`EngineIdSource`.
    """
    if ordinal < 0:
        raise ValueError("ID ordinal must not be negative")
    source = EngineIdSource(seed)
    source._counter = ordinal
    return source.next_id(kind)


def _seed_bytes(seed: str | int | bytes) -> bytes:
    if isinstance(seed, bytes):
        return seed
    if isinstance(seed, int):
        return f"int:{seed}".encode()
    return f"str:{seed}".encode()


__all__ = [
    "DeterministicIdSource",
    "EngineIdFactory",
    "EngineIdSource",
    "SeededIdSource",
    "deterministic_id",
]
