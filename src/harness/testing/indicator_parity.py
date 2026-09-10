"""Cross-mode parity oracle for tick-native indicators.

This module is the indicator analog of the determinism harness and the
paper-parity harness.  It proves that one canonical quote/trade sequence
produces byte-identical indicator outputs when that sequence is delivered in
each execution mode under identical kernel orderings:

- ``streamed`` — the reference: events are fed one at a time in canonical
  kernel order directly into an :class:`IndicatorRouter`.
- ``bus_replay`` — the backtest/paper path: events are published to a
  deterministic :class:`EventBus` in deliberately reordered arrival chunks
  and drained between chunks, so the kernel's priority queue restores the
  canonical dispatch order before any indicator sees an event.
- ``snapshot_resume`` — the live-restart path: the stream runs to completion
  while the basket is snapshotted and restored into a fresh router every
  ``resume_every`` events, so outputs must be invariant to mid-stream
  restarts.

The harness compares full sample sequences (exact values and timestamps),
final in-progress values, and final basket snapshot bytes across every mode,
and re-runs the reference stream to pin determinism.  Divergence is reported
structurally; :func:`assert_indicator_parity` turns any divergence into a
test failure.  No wall clock, randomness, network, or I/O is involved.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from harness.configuration import canonical_hash
from harness.core.clock import SimClock
from harness.core.events import EVENT_CLASS_PRIORITY, EventBus
from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.indicators import (
    IndicatorRouter,
    IndicatorSample,
    TickIndicator,
    create_indicator,
)

SCENARIO_SCHEMA: Final = "fca-indicator-parity-scenario-v1"


class IndicatorParityError(AssertionError):
    """Raised when indicator outputs diverge across execution modes."""


class IndicatorParityConfigurationError(ValueError):
    """Raised when a parity scenario is misconfigured."""


class ParityMode(StrEnum):
    """Execution modes compared by the parity oracle."""

    STREAMED = "streamed"
    BUS_REPLAY = "bus_replay"
    SNAPSHOT_RESUME = "snapshot_resume"
    DETERMINISM_RERUN = "determinism_rerun"


@dataclass(frozen=True, slots=True)
class IndicatorRequest:
    """One declarative indicator request bound to one canonical symbol."""

    name: str
    symbol: str | Symbol
    params: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise IndicatorParityConfigurationError(
                "indicator request name must be a non-empty string"
            )
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        if not isinstance(self.params, Mapping):
            raise IndicatorParityConfigurationError(
                "indicator request params must be a mapping"
            )
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


@dataclass(frozen=True, slots=True)
class IndicatorParityDimension:
    """One pinned comparison dimension and its first divergence, if any."""

    name: str
    matched: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class IndicatorParityScenario:
    """Complete deterministic input for one indicator parity run.

    ``events`` is the canonical quote/trade sequence; it must already be in
    kernel dispatch order ``(timestamp, sequence, event-class priority)``
    because the streamed reference consumes it directly.  ``jitter`` is the
    arrival-chunk size for bus replay (each chunk is published in reversed
    order so the kernel queue provably reorders it); ``resume_every`` is the
    snapshot/restore cadence; ``session_anchor`` is passed to every indicator
    so session semantics are pinned scenario-wide.
    """

    events: Sequence[QuoteEvent | TradeEvent]
    requests: Sequence[IndicatorRequest]
    jitter: int = 4
    resume_every: int = 1
    session_anchor: time | None = None

    def __post_init__(self) -> None:
        events = tuple(self.events)
        if not events:
            raise IndicatorParityConfigurationError("events must not be empty")
        if not all(isinstance(event, QuoteEvent | TradeEvent) for event in events):
            raise IndicatorParityConfigurationError(
                "events must contain QuoteEvent or TradeEvent values"
            )
        previous: tuple[datetime, int, int] | None = None
        for event in events:
            key = (
                event.timestamp,
                event.sequence,
                EVENT_CLASS_PRIORITY[event.event_type],
            )
            if previous is not None and key < previous:
                raise IndicatorParityConfigurationError(
                    "events must already be in kernel dispatch order "
                    "(timestamp, sequence, event-class priority)"
                )
            previous = key
        object.__setattr__(self, "events", events)
        requests = tuple(self.requests)
        if not requests or not all(
            isinstance(request, IndicatorRequest) for request in requests
        ):
            raise IndicatorParityConfigurationError(
                "requests must contain IndicatorRequest values"
            )
        object.__setattr__(self, "requests", requests)
        for name, value in (
            ("jitter", self.jitter),
            ("resume_every", self.resume_every),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise IndicatorParityConfigurationError(
                    f"{name} must be a positive integer"
                )
        anchor = self.session_anchor
        if anchor is not None and (
            not isinstance(anchor, time) or anchor.tzinfo is not None
        ):
            raise IndicatorParityConfigurationError(
                "session_anchor must be a naive datetime.time or None"
            )

    @property
    def scenario_hash(self) -> str:
        """Return the canonical hash of every pinned scenario input."""
        return canonical_hash(
            {
                "schema": SCENARIO_SCHEMA,
                "events": [
                    {item.name: getattr(event, item.name) for item in fields(event)}
                    for event in self.events
                ],
                "requests": [
                    {
                        "name": request.name,
                        "symbol": str(request.symbol),
                        "params": dict(request.params),
                    }
                    for request in self.requests
                ],
                "jitter": self.jitter,
                "resume_every": self.resume_every,
                "session_anchor": (
                    None
                    if self.session_anchor is None
                    else self.session_anchor.isoformat()
                ),
            }
        )

    def _build(self) -> tuple[TickIndicator, ...]:
        return tuple(
            create_indicator(
                request.name,
                dict(request.params),
                symbol=request.symbol,
                session_anchor=self.session_anchor,
            )
            for request in self.requests
        )

    def run(self) -> IndicatorParityReport:
        """Run every mode over the canonical stream and compare outputs."""
        return run_indicator_parity(self)

    def assert_clean(self) -> IndicatorParityReport:
        """Run the scenario and raise :class:`IndicatorParityError` on divergence."""
        return assert_indicator_parity(self)


@dataclass(frozen=True, slots=True)
class _ModeOutput:
    samples: tuple[IndicatorSample, ...]
    values: tuple[Decimal | None, ...]
    snapshot: bytes


@dataclass(frozen=True, slots=True)
class IndicatorParityReport:
    """Structured parity outcome naming every pinned dimension."""

    scenario_hash: str
    dimensions: tuple[IndicatorParityDimension, ...]
    sample_count: int

    @property
    def parity_clean(self) -> bool:
        """Return whether every pinned dimension matched."""
        return all(dimension.matched for dimension in self.dimensions)

    @property
    def divergences(self) -> tuple[str, ...]:
        """Return every diverging dimension name in canonical order."""
        return tuple(
            dimension.name for dimension in self.dimensions if not dimension.matched
        )

    def summary(self) -> str:
        """Return a one-line human-readable parity summary."""
        if self.parity_clean:
            return (
                f"indicator parity clean: {len(self.dimensions)} pinned dimensions "
                f"matched over {self.sample_count} samples"
            )
        failures = "; ".join(
            f"{dimension.name}: {dimension.detail}"
            for dimension in self.dimensions
            if not dimension.matched
        )
        return f"indicator parity diverged: {failures}"

    def assert_clean(self) -> IndicatorParityReport:
        """Raise :class:`IndicatorParityError` when any dimension diverges."""
        if not self.parity_clean:
            raise IndicatorParityError(self.summary())
        return self


def _capture(router: IndicatorRouter) -> _ModeOutput:
    return _ModeOutput(
        samples=router.samples(),
        values=tuple(indicator.value for indicator in router.indicators),
        snapshot=router.snapshot(),
    )


def _run_streamed(
    events: Sequence[QuoteEvent | TradeEvent],
    build: Callable[[], tuple[TickIndicator, ...]],
) -> _ModeOutput:
    router = IndicatorRouter(*build())
    router.on_events(events)
    router.flush()
    return _capture(router)


def _run_bus_replay(
    events: Sequence[QuoteEvent | TradeEvent],
    build: Callable[[], tuple[TickIndicator, ...]],
    jitter: int,
) -> _ModeOutput:
    clock = SimClock(events[0].timestamp)
    bus = EventBus(clock)
    router = IndicatorRouter(*build())
    bus.subscribe(QuoteEvent, router.on_event)
    bus.subscribe(TradeEvent, router.on_event)
    for start in range(0, len(events), jitter):
        chunk = events[start : start + jitter]
        bus.publish_many(tuple(reversed(chunk)))
        bus.drain()
    router.flush()
    return _capture(router)


def _run_snapshot_resume(
    events: Sequence[QuoteEvent | TradeEvent],
    build: Callable[[], tuple[TickIndicator, ...]],
    resume_every: int,
) -> _ModeOutput:
    router = IndicatorRouter(*build())
    total = len(events)
    for index, event in enumerate(events, start=1):
        router.on_event(event)
        if index % resume_every == 0 and index < total:
            payload = router.snapshot()
            router = IndicatorRouter(*build())
            router.restore(payload)
    router.flush()
    return _capture(router)


def _difference(
    left: Sequence[object], right: Sequence[object], label: str
) -> str | None:
    if len(left) != len(right):
        return f"{label}: length {len(left)} != {len(right)}"
    for index, (first, second) in enumerate(zip(left, right, strict=True)):
        if first != second:
            return f"{label}[{index}]: {first!r} != {second!r}"
    return None


def _mode_dimension(
    mode: ParityMode, reference: _ModeOutput, candidate: _ModeOutput
) -> IndicatorParityDimension:
    detail = _difference(
        reference.samples, candidate.samples, f"mode:{mode.value} samples"
    ) or _difference(reference.values, candidate.values, f"mode:{mode.value} values")
    return IndicatorParityDimension(f"mode:{mode.value}", detail is None, detail)


def _snapshot_dimension(
    reference: _ModeOutput, candidates: Sequence[tuple[str, _ModeOutput]]
) -> IndicatorParityDimension:
    for label, output in candidates:
        if output.snapshot != reference.snapshot:
            return IndicatorParityDimension(
                "snapshot_bytes", False, f"{label} snapshot bytes differ"
            )
    return IndicatorParityDimension("snapshot_bytes", True, None)


def run_indicator_parity(scenario: IndicatorParityScenario) -> IndicatorParityReport:
    """Run every delivery mode over the scenario stream and compare outputs."""
    if not isinstance(scenario, IndicatorParityScenario):
        raise TypeError("scenario must be an IndicatorParityScenario")
    events = scenario.events
    reference = _run_streamed(events, scenario._build)
    bus = _run_bus_replay(events, scenario._build, scenario.jitter)
    resumed = _run_snapshot_resume(events, scenario._build, scenario.resume_every)
    rerun = _run_streamed(events, scenario._build)
    dimensions = (
        _mode_dimension(ParityMode.BUS_REPLAY, reference, bus),
        _mode_dimension(ParityMode.SNAPSHOT_RESUME, reference, resumed),
        _snapshot_dimension(
            reference,
            (
                ("bus_replay", bus),
                ("snapshot_resume", resumed),
                ("determinism_rerun", rerun),
            ),
        ),
        _mode_dimension(ParityMode.DETERMINISM_RERUN, reference, rerun),
    )
    return IndicatorParityReport(
        scenario_hash=scenario.scenario_hash,
        dimensions=dimensions,
        sample_count=len(reference.samples),
    )


def assert_indicator_parity(
    scenario: IndicatorParityScenario,
) -> IndicatorParityReport:
    """Run the scenario and raise :class:`IndicatorParityError` on divergence."""
    return run_indicator_parity(scenario).assert_clean()


__all__ = [
    "IndicatorParityConfigurationError",
    "IndicatorParityDimension",
    "IndicatorParityError",
    "IndicatorParityReport",
    "IndicatorParityScenario",
    "IndicatorRequest",
    "ParityMode",
    "assert_indicator_parity",
    "run_indicator_parity",
]
