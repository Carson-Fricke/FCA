"""Event-driven tick indicator protocol and the shared state machine.

Indicators in this package are tick-native: they consume kernel-dispatched
``QuoteEvent`` and ``TradeEvent`` values in event time, maintain
O(1)-amortized incremental state, and never read a wall clock, recompute over
a historical window, or consult an external data source.

Emission boundaries reuse the kernel's bar-derivation logic exactly: event
time is floored onto half-open ``[start, end)`` buckets anchored at the Unix
epoch (see :func:`bucket_start`, the mirror of ``harness.data.bars``).  A
bucket is closed when the first accepted event lands in a strictly later
bucket, and the closed sample is stamped with the bucket's exclusive end, so
each sample reflects exactly the driver events in
``[end - interval, end)`` and never leaks a newer event.  Buckets with no
driver event emit nothing, mirroring the kernel's non-empty bars, and
:meth:`TickIndicator.flush` closes the trailing open bucket at the end of a
finite replay.

Sessions are derived from event time alone: cumulative state resets when an
event crosses the configured UTC time-of-day anchor (midnight by default),
and ``session_anchor=None`` keeps state continuous.  When a session boundary
falls inside a bucket interval, the bucket yields two samples: the
pre-boundary state closed early and the post-boundary state closed at the
bucket's exclusive end, both stamped with that exclusive end.

Point-in-time safety is structural: events are accepted only in non-decreasing
kernel order ``(timestamp, sequence)``, so an indicator's value and samples
can only ever reflect events that were already accepted, and rewinding is
possible only by restoring an explicit :meth:`TickIndicator.snapshot`.

All indicator arithmetic runs inside :data:`ARITHMETIC_CONTEXT`, a pinned
``decimal.Context`` with fixed precision, so results are bit-identical across
backtest replay, paper, and live replays of the same event ordering
regardless of any ambient decimal context.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Context, Decimal, InvalidOperation
from typing import ClassVar, Final

from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.common import text, timestamp
from harness.core.types.events import EventType, QuoteEvent, TradeEvent
from harness.data.bars import interval_delta

ARITHMETIC_PRECISION: Final = 34
ARITHMETIC_CONTEXT: Final = Context(prec=ARITHMETIC_PRECISION)
MIDNIGHT_UTC: Final = time(0, 0)
SNAPSHOT_SCHEMA: Final = "fca-indicator-snapshot-v1"
BASKET_SNAPSHOT_SCHEMA: Final = "fca-indicator-basket-snapshot-v1"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_ZERO: Final = Decimal("0")
_TWO: Final = Decimal("2")
_HUNDRED: Final = Decimal("100")
_MICROSECOND: Final = timedelta(microseconds=1)


class IndicatorConfigurationError(ValueError):
    """Raised when an indicator is constructed or registered incorrectly."""


class IndicatorOrderingError(ValueError):
    """Raised when an event regresses the accepted kernel ordering."""


class IndicatorSnapshotError(ValueError):
    """Raised when a snapshot payload is invalid or does not match."""


def add(left: Decimal, right: Decimal) -> Decimal:
    """Add two decimals inside the pinned arithmetic context."""
    return ARITHMETIC_CONTEXT.add(left, right)


def subtract(left: Decimal, right: Decimal) -> Decimal:
    """Subtract two decimals inside the pinned arithmetic context."""
    return ARITHMETIC_CONTEXT.subtract(left, right)


def multiply(left: Decimal, right: Decimal) -> Decimal:
    """Multiply two decimals inside the pinned arithmetic context."""
    return ARITHMETIC_CONTEXT.multiply(left, right)


def divide(left: Decimal, right: Decimal) -> Decimal:
    """Divide two decimals inside the pinned arithmetic context."""
    return ARITHMETIC_CONTEXT.divide(left, right)


def sqrt(value: Decimal) -> Decimal:
    """Take the correctly rounded square root inside the pinned context."""
    return ARITHMETIC_CONTEXT.sqrt(value)


def bucket_start(value: datetime, interval: timedelta) -> datetime:
    """Floor ``value`` onto the half-open bucket grid anchored at the epoch.

    This mirrors ``harness.data.bars._bucket_start`` so indicator emission
    boundaries and kernel bar boundaries always coincide.
    """
    if not isinstance(interval, timedelta) or interval <= timedelta(0):
        raise ValueError("interval must be a positive timedelta")
    elapsed = timestamp(value, "value") - _EPOCH
    return _EPOCH + (elapsed // interval) * interval


@dataclass(frozen=True, slots=True)
class IndicatorSample:
    """One emitted indicator value for a closed half-open event-time bucket.

    ``start`` is the inclusive bucket start, ``end`` the exclusive bucket end
    that doubles as the emission stamp, and ``value`` the indicator value as
    of that boundary.  The value reflects exactly the accepted events in
    ``[start, end)`` plus any longer-window state that survived to ``end``.
    """

    name: str
    symbol: Symbol
    start: datetime
    end: datetime
    value: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", text(self.name, "name"))
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(self, "start", timestamp(self.start, "start"))
        object.__setattr__(self, "end", timestamp(self.end, "end"))
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if not isinstance(self.value, Decimal) or not self.value.is_finite():
            raise ValueError("value must be a finite Decimal")


def _validated_anchor(anchor: time | None) -> time | None:
    if anchor is None:
        return None
    if not isinstance(anchor, time):
        raise TypeError("session_anchor must be a datetime.time or None")
    if anchor.tzinfo is not None:
        raise ValueError(
            "session_anchor must be a naive time interpreted on the UTC timeline"
        )
    return anchor


def _positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _decode_json(payload: bytes) -> object:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IndicatorSnapshotError("snapshot payload is not valid JSON") from error


def _snapshot_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise IndicatorSnapshotError(f"snapshot {field_name} must be an ISO string")
    try:
        return timestamp(datetime.fromisoformat(value), field_name)
    except (TypeError, ValueError) as error:
        raise IndicatorSnapshotError(
            f"snapshot {field_name} is not a valid timestamp"
        ) from error


def _snapshot_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise IndicatorSnapshotError(f"snapshot {field_name} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise IndicatorSnapshotError(
            f"snapshot {field_name} is not a valid decimal"
        ) from error
    if not result.is_finite():
        raise IndicatorSnapshotError(f"snapshot {field_name} must be finite")
    return result


def _snapshot_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IndicatorSnapshotError(f"snapshot {field_name} must be an integer")
    return value


class TickIndicator(ABC):
    """Base state machine for one symbol's tick-native indicator.

    Subclasses declare :data:`NAME` (the registry name), :data:`CONSUMES` (the
    event classes routed to them), and :data:`DRIVERS` (the subset whose
    events count toward bucket emission and warm-up).  They implement the
    ``_on_trade`` / ``_on_quote`` incremental hooks and ``_compute_value``,
    and may override the bucket and session hooks.  Every instance is bound
    to exactly one canonical symbol.
    """

    NAME: ClassVar[str]
    CONSUMES: ClassVar[tuple[EventType, ...]] = (EventType.QUOTE, EventType.TRADE)
    DRIVERS: ClassVar[tuple[EventType, ...]] = (EventType.QUOTE, EventType.TRADE)

    __slots__ = (
        "_bucket_driver_count",
        "_interval",
        "_last_key",
        "_open_bucket",
        "_samples",
        "_session_anchor",
        "_session_start",
        "_symbol",
    )

    def __init__(
        self,
        symbol: str | Symbol,
        *,
        bucket: timedelta | str = "1min",
        session_anchor: time | None = MIDNIGHT_UTC,
    ) -> None:
        if not isinstance(self.NAME, str) or not self.NAME.strip():
            raise IndicatorConfigurationError(
                "indicator NAME must be a non-empty string"
            )
        if not self.DRIVERS or not set(self.DRIVERS) <= set(self.CONSUMES):
            raise IndicatorConfigurationError(
                "DRIVERS must be a non-empty subset of CONSUMES"
            )
        self._symbol = canonical_symbol(symbol)
        self._interval = interval_delta(bucket)
        self._session_anchor = _validated_anchor(session_anchor)
        self._samples: list[IndicatorSample] = []
        self._open_bucket: datetime | None = None
        self._bucket_driver_count = 0
        self._last_key: tuple[datetime, int] | None = None
        self._session_start: datetime | None = None

    @property
    def symbol(self) -> Symbol:
        """Return the canonical symbol this indicator is bound to."""
        return self._symbol

    @property
    def interval(self) -> timedelta:
        """Return the exact emission-bucket interval."""
        return self._interval

    @property
    def session_anchor(self) -> time | None:
        """Return the UTC session anchor, or ``None`` when sessions are off."""
        return self._session_anchor

    @property
    def value(self) -> Decimal | None:
        """Return the in-progress value as of the last accepted event.

        The read reflects only accepted events, so it is point-in-time safe
        by construction; it may still include window state that a later
        bucket close would expire.
        """
        return self._compute_value()

    def samples(self) -> tuple[IndicatorSample, ...]:
        """Return every sample emitted so far, in emission order."""
        return tuple(self._samples)

    def flush(self) -> None:
        """Close the open bucket, emitting its sample when one is defined.

        This is an end-of-replay convenience and is idempotent; feeding more
        events afterwards reopens a bucket on the same grid.
        """
        self._close_open_bucket()

    def on_event(self, event: QuoteEvent | TradeEvent) -> None:
        """Accept one kernel-dispatched market event for the bound symbol.

        Events must arrive in non-decreasing ``(timestamp, sequence)`` kernel
        order; the call closes and emits any bucket the event rotates, applies
        session rollover, then folds the event into the incremental state.
        """
        if not isinstance(event, QuoteEvent | TradeEvent):
            raise TypeError("event must be a QuoteEvent or TradeEvent")
        if event.event_type not in self.CONSUMES:
            return
        if canonical_symbol(event.symbol) != self._symbol:
            raise ValueError(
                f"event symbol {event.symbol!r} does not match indicator "
                f"symbol {self._symbol!r}"
            )
        key = (event.timestamp, event.sequence)
        if self._last_key is not None and key < self._last_key:
            raise IndicatorOrderingError(
                f"event {event.event_id!r} regresses the accepted ordering"
            )
        bucket = bucket_start(event.timestamp, self._interval)
        if self._open_bucket is not None and bucket > self._open_bucket:
            self._close_open_bucket()
        if self._session_anchor is not None:
            session = self._session_for(event.timestamp)
            if self._session_start is None:
                self._session_start = session
            elif session > self._session_start:
                if self._open_bucket is not None:
                    self._close_open_bucket()
                self._reset_session()
                self._session_start = session
        if self._open_bucket is None:
            self._open_bucket = bucket
            self._bucket_driver_count = 0
            self._on_bucket_open(bucket)
        if isinstance(event, TradeEvent):
            self._on_trade(event)
        else:
            self._on_quote(event)
        if event.event_type in self.DRIVERS:
            self._bucket_driver_count += 1
        self._last_key = key

    def snapshot(self) -> bytes:
        """Return the canonical snapshot payload for this indicator."""
        payload = {
            "schema": SNAPSHOT_SCHEMA,
            "name": self.NAME,
            "params": self._params(),
            "state": {
                "runtime": self._runtime_state(),
                "indicator": self._snapshot_state(),
            },
        }
        return _canonical_json_bytes(payload)

    def restore(self, payload: bytes) -> None:
        """Restore state from a canonical snapshot payload.

        The payload must carry the same indicator name and construction
        parameters as this instance; otherwise :class:`IndicatorSnapshotError`
        is raised and no state is mutated.
        """
        parsed = _decode_json(payload)
        if not isinstance(parsed, dict) or parsed.get("schema") != SNAPSHOT_SCHEMA:
            raise IndicatorSnapshotError("snapshot payload schema is not recognized")
        if parsed.get("name") != self.NAME:
            raise IndicatorSnapshotError(
                f"snapshot name {parsed.get('name')!r} does not match {self.NAME!r}"
            )
        params = parsed.get("params")
        if not isinstance(params, dict) or params != self._params():
            raise IndicatorSnapshotError(
                "snapshot parameters do not match this indicator"
            )
        state = parsed.get("state")
        if not isinstance(state, dict):
            raise IndicatorSnapshotError("snapshot state is missing")
        runtime = state.get("runtime")
        indicator_state = state.get("indicator")
        if not isinstance(runtime, dict) or not isinstance(indicator_state, dict):
            raise IndicatorSnapshotError("snapshot state is malformed")
        self._restore_runtime(runtime)
        self._restore_state(indicator_state)

    @abstractmethod
    def _compute_value(self) -> Decimal | None:
        """Return the current indicator value, or ``None`` before warm-up."""

    def _on_trade(self, trade: TradeEvent) -> None:
        """Fold one accepted trade into the incremental state."""
        return None

    def _on_quote(self, quote: QuoteEvent) -> None:
        """Fold one accepted quote into the incremental state."""
        return None

    def _on_bucket_open(self, bucket: datetime) -> None:
        """Reset per-bucket accumulators when a new bucket opens."""
        return None

    def _on_bucket_close(self, end: datetime) -> None:
        """Expire or aggregate state against a closing bucket boundary."""
        return None

    def _reset_session(self) -> None:
        """Reset per-session cumulative state when a session rolls over."""
        return None

    def _indicator_params(self) -> dict[str, object]:
        """Return the JSON-safe indicator-specific construction parameters."""
        return {}

    def _snapshot_state(self) -> dict[str, object]:
        """Return the JSON-safe indicator-specific state."""
        return {}

    def _restore_state(self, state: dict[str, object]) -> None:
        if state:
            raise IndicatorSnapshotError(
                f"snapshot carries unexpected indicator state: {sorted(state)}"
            )

    def _close_open_bucket(self) -> None:
        bucket = self._open_bucket
        if bucket is None:
            return
        end = bucket + self._interval
        self._on_bucket_close(end)
        if self._bucket_driver_count > 0:
            value = self._compute_value()
            if value is not None:
                self._samples.append(
                    IndicatorSample(self.NAME, self._symbol, bucket, end, value)
                )
        self._open_bucket = None
        self._bucket_driver_count = 0

    def _session_for(self, moment: datetime) -> datetime:
        anchor = self._session_anchor
        if anchor is None:
            raise RuntimeError("session boundaries are disabled for this indicator")
        start = moment.replace(
            hour=anchor.hour,
            minute=anchor.minute,
            second=anchor.second,
            microsecond=anchor.microsecond,
        )
        if moment.time() < anchor:
            start -= timedelta(days=1)
        return start

    def _params(self) -> dict[str, object]:
        params: dict[str, object] = {
            "symbol": str(self._symbol),
            "bucket": self._interval // _MICROSECOND,
            "session_anchor": (
                None
                if self._session_anchor is None
                else self._session_anchor.isoformat()
            ),
        }
        params.update(self._indicator_params())
        return params

    def _runtime_state(self) -> dict[str, object]:
        return {
            "samples": [
                {
                    "start": sample.start.isoformat(),
                    "end": sample.end.isoformat(),
                    "value": str(sample.value),
                }
                for sample in self._samples
            ],
            "open_bucket": (
                None if self._open_bucket is None else self._open_bucket.isoformat()
            ),
            "bucket_driver_count": self._bucket_driver_count,
            "last_key": (
                None
                if self._last_key is None
                else {
                    "timestamp": self._last_key[0].isoformat(),
                    "sequence": self._last_key[1],
                }
            ),
            "session_start": (
                None if self._session_start is None else self._session_start.isoformat()
            ),
        }

    def _restore_runtime(self, state: dict[str, object]) -> None:
        raw_samples = state.get("samples")
        if not isinstance(raw_samples, list):
            raise IndicatorSnapshotError("snapshot samples must be a list")
        restored: list[IndicatorSample] = []
        for entry in raw_samples:
            if not isinstance(entry, dict):
                raise IndicatorSnapshotError("snapshot sample entries must be objects")
            restored.append(
                IndicatorSample(
                    self.NAME,
                    self._symbol,
                    _snapshot_datetime(entry.get("start"), "sample start"),
                    _snapshot_datetime(entry.get("end"), "sample end"),
                    _snapshot_decimal(entry.get("value"), "sample value"),
                )
            )
        count = _snapshot_int(state.get("bucket_driver_count"), "bucket_driver_count")
        if count < 0:
            raise IndicatorSnapshotError("bucket_driver_count must be non-negative")
        raw_open_bucket = state.get("open_bucket")
        raw_last_key = state.get("last_key")
        restored_key: tuple[datetime, int] | None = None
        if raw_last_key is not None:
            if not isinstance(raw_last_key, dict):
                raise IndicatorSnapshotError("snapshot last_key must be an object")
            restored_key = (
                _snapshot_datetime(raw_last_key.get("timestamp"), "last_key timestamp"),
                _snapshot_int(raw_last_key.get("sequence"), "last_key sequence"),
            )
        raw_session_start = state.get("session_start")
        self._samples = restored
        self._open_bucket = (
            None
            if raw_open_bucket is None
            else _snapshot_datetime(raw_open_bucket, "open_bucket")
        )
        self._bucket_driver_count = count
        self._last_key = restored_key
        self._session_start = (
            None
            if raw_session_start is None
            else _snapshot_datetime(raw_session_start, "session_start")
        )


class IndicatorRouter:
    """Fan out market events to same-symbol indicators and collect samples.

    The router is the strategy-facing composition point: attach one indicator
    per ``(name, params, symbol)`` request, feed it the kernel-dispatched
    stream, and read flattened samples in attachment order.  It can also take
    and restore a canonical basket snapshot for mid-stream restarts.
    """

    __slots__ = ("_indicators",)

    def __init__(self, *indicators: TickIndicator) -> None:
        self._indicators: list[TickIndicator] = []
        for indicator in indicators:
            self.attach(indicator)

    @property
    def indicators(self) -> tuple[TickIndicator, ...]:
        """Return attached indicators in attachment order."""
        return tuple(self._indicators)

    def attach(self, indicator: TickIndicator) -> None:
        """Attach one indicator, rejecting duplicate instances."""
        if not isinstance(indicator, TickIndicator):
            raise TypeError("indicator must be a TickIndicator")
        if any(existing is indicator for existing in self._indicators):
            raise IndicatorConfigurationError("indicator is already attached")
        self._indicators.append(indicator)

    def on_event(self, event: QuoteEvent | TradeEvent) -> None:
        """Route one market event to every attached indicator that consumes it."""
        if not isinstance(event, QuoteEvent | TradeEvent):
            raise TypeError("event must be a QuoteEvent or TradeEvent")
        symbol = canonical_symbol(event.symbol)
        for indicator in self._indicators:
            if indicator.symbol == symbol and event.event_type in indicator.CONSUMES:
                indicator.on_event(event)

    def on_events(self, events: Iterable[QuoteEvent | TradeEvent]) -> None:
        """Route events in iteration order."""
        for event in events:
            self.on_event(event)

    def flush(self) -> None:
        """Flush every attached indicator."""
        for indicator in self._indicators:
            indicator.flush()

    def samples(self) -> tuple[IndicatorSample, ...]:
        """Return all samples in attachment order, then emission order."""
        return tuple(
            sample for indicator in self._indicators for sample in indicator.samples()
        )

    def snapshot(self) -> bytes:
        """Return the canonical basket snapshot of every attached indicator."""
        entries = [
            json.loads(indicator.snapshot().decode("utf-8"))
            for indicator in self._indicators
        ]
        return _canonical_json_bytes(
            {"schema": BASKET_SNAPSHOT_SCHEMA, "indicators": entries}
        )

    def restore(self, payload: bytes) -> None:
        """Restore every attached indicator from a canonical basket snapshot."""
        parsed = _decode_json(payload)
        if (
            not isinstance(parsed, dict)
            or parsed.get("schema") != BASKET_SNAPSHOT_SCHEMA
        ):
            raise IndicatorSnapshotError("basket snapshot schema is not recognized")
        entries = parsed.get("indicators")
        if not isinstance(entries, list) or len(entries) != len(self._indicators):
            raise IndicatorSnapshotError(
                "basket snapshot does not match the attached indicators"
            )
        for indicator, entry in zip(self._indicators, entries, strict=False):
            if not isinstance(entry, dict):
                raise IndicatorSnapshotError("basket snapshot entries must be objects")
            indicator.restore(_canonical_json_bytes(entry))


__all__ = [
    "ARITHMETIC_CONTEXT",
    "ARITHMETIC_PRECISION",
    "BASKET_SNAPSHOT_SCHEMA",
    "IndicatorConfigurationError",
    "IndicatorOrderingError",
    "IndicatorRouter",
    "IndicatorSample",
    "IndicatorSnapshotError",
    "MIDNIGHT_UTC",
    "SNAPSHOT_SCHEMA",
    "TickIndicator",
    "add",
    "bucket_start",
    "divide",
    "multiply",
    "sqrt",
    "subtract",
]
