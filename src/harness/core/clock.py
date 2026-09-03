"""Explicit clock boundaries for deterministic and live event processing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from harness.core.types.common import timestamp


@runtime_checkable
class Clock(Protocol):
    """Source of the current, timezone-aware engine timestamp."""

    def now(self) -> datetime:
        """Return the current timestamp normalized to UTC."""
        ...


@runtime_checkable
class AdvanceableClock(Clock, Protocol):
    """Clock whose head can move forward under control of the event kernel."""

    def advance_to(self, value: datetime) -> datetime:
        """Move to ``value`` and return the new head."""
        ...


class SimClock:
    """A deterministic clock that only moves monotonically forward."""

    __slots__ = ("_head",)

    def __init__(self, start: datetime) -> None:
        self._head = timestamp(start, "start")

    def now(self) -> datetime:
        """Return the simulation head."""
        return self._head

    @property
    def head(self) -> datetime:
        """Return the simulation head without consulting wall time."""
        return self._head

    def advance_to(self, value: datetime) -> datetime:
        """Move the head forward, rejecting timestamps older than the head."""
        target = timestamp(value)
        if target < self._head:
            raise ValueError("timestamp cannot be older than the SimClock head")
        self._head = target
        return self._head

    def advance_by(self, delta: timedelta) -> datetime:
        """Move the head by a non-negative duration."""
        if not isinstance(delta, timedelta):
            raise TypeError("delta must be a timedelta")
        if delta < timedelta(0):
            raise ValueError("delta must not be negative")
        return self.advance_to(self._head + delta)


class WallClock:
    """Live adapter that explicitly owns access to the system clock."""

    __slots__ = ()

    def now(self) -> datetime:
        """Return the current UTC wall-clock timestamp."""
        return datetime.now(UTC)


__all__ = ["AdvanceableClock", "Clock", "SimClock", "WallClock"]
