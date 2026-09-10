"""Volatility indicators computed incrementally from tick events.

Emission, ordering, session, and snapshot semantics come from
:class:`~harness.indicators.base.TickIndicator`.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import ClassVar, Final

from harness.core.symbols import Symbol
from harness.core.types.events import EventType, TradeEvent
from harness.data.bars import interval_delta
from harness.indicators.base import (
    MIDNIGHT_UTC,
    IndicatorSnapshotError,
    TickIndicator,
    _positive_int,
    _snapshot_datetime,
    _snapshot_decimal,
    _snapshot_int,
    add,
    divide,
    multiply,
    sqrt,
    subtract,
)

_ZERO: Final = Decimal("0")


class AverageTrueRange(TickIndicator):
    """Wilder ATR over per-bucket true ranges derived from trade ticks.

    Each bucket accumulates its high, low, and close from accepted trades.
    When the bucket closes, its true range is
    ``max(high - low, |high - previous_close|, |low - previous_close|)``,
    falling back to ``high - low`` for the first range.  The first ``period``
    ranges seed a simple mean and later ranges apply Wilder smoothing, so the
    value warms up after ``period`` trade-bearing buckets.  Buckets without
    trades are skipped, and ``previous_close`` carries across them and across
    session boundaries.
    """

    NAME: ClassVar[str] = "atr"
    CONSUMES: ClassVar[tuple[EventType, ...]] = (EventType.QUOTE, EventType.TRADE)
    DRIVERS: ClassVar[tuple[EventType, ...]] = (EventType.TRADE,)

    __slots__ = (
        "_avg_range",
        "_close",
        "_high",
        "_low",
        "_period",
        "_previous_close",
        "_range_total",
        "_ranges",
    )

    def __init__(
        self,
        symbol: str | Symbol,
        *,
        period: int,
        bucket: timedelta | str = "1min",
        session_anchor: time | None = MIDNIGHT_UTC,
    ) -> None:
        super().__init__(symbol, bucket=bucket, session_anchor=session_anchor)
        self._period = _positive_int(period, "period")
        self._high: Decimal | None = None
        self._low: Decimal | None = None
        self._close: Decimal | None = None
        self._previous_close: Decimal | None = None
        self._ranges = 0
        self._range_total = _ZERO
        self._avg_range: Decimal | None = None

    def _on_bucket_open(self, bucket: datetime) -> None:
        self._high = None
        self._low = None
        self._close = None

    def _on_trade(self, trade: TradeEvent) -> None:
        price = trade.price
        if self._high is None or self._low is None:
            self._high = price
            self._low = price
        else:
            if price > self._high:
                self._high = price
            if price < self._low:
                self._low = price
        self._close = price

    def _on_bucket_close(self, end: datetime) -> None:
        high = self._high
        low = self._low
        close = self._close
        if high is None or low is None or close is None:
            return
        previous_close = self._previous_close
        true_range = subtract(high, low)
        if previous_close is not None:
            high_gap = subtract(high, previous_close)
            low_gap = subtract(low, previous_close)
            if high_gap > true_range:
                true_range = high_gap
            if -low_gap > true_range:
                true_range = -low_gap
        self._previous_close = close
        if self._avg_range is None:
            self._range_total = add(self._range_total, true_range)
            self._ranges += 1
            if self._ranges == self._period:
                self._avg_range = divide(self._range_total, Decimal(self._period))
        else:
            self._avg_range = add(
                self._avg_range,
                divide(subtract(true_range, self._avg_range), Decimal(self._period)),
            )

    def _compute_value(self) -> Decimal | None:
        return self._avg_range

    def _indicator_params(self) -> dict[str, object]:
        return {"period": self._period}

    def _snapshot_state(self) -> dict[str, object]:
        return {
            "avg_range": None if self._avg_range is None else str(self._avg_range),
            "close": None if self._close is None else str(self._close),
            "high": None if self._high is None else str(self._high),
            "low": None if self._low is None else str(self._low),
            "previous_close": (
                None if self._previous_close is None else str(self._previous_close)
            ),
            "range_total": str(self._range_total),
            "ranges": self._ranges,
        }

    def _restore_state(self, state: dict[str, object]) -> None:
        ranges = _snapshot_int(state.get("ranges"), "atr ranges")
        if ranges < 0:
            raise IndicatorSnapshotError("snapshot atr ranges must be non-negative")
        raw_avg_range = state.get("avg_range")
        self._avg_range = (
            None
            if raw_avg_range is None
            else _snapshot_decimal(raw_avg_range, "atr avg_range")
        )
        if (ranges >= self._period) != (self._avg_range is not None):
            raise IndicatorSnapshotError("snapshot atr ranges and avg_range disagree")
        self._ranges = ranges
        self._range_total = _snapshot_decimal(
            state.get("range_total"), "atr range_total"
        )
        self._high = self._optional_decimal(state.get("high"), "atr high")
        self._low = self._optional_decimal(state.get("low"), "atr low")
        self._close = self._optional_decimal(state.get("close"), "atr close")
        self._previous_close = self._optional_decimal(
            state.get("previous_close"), "atr previous_close"
        )

    @staticmethod
    def _optional_decimal(value: object, field_name: str) -> Decimal | None:
        return None if value is None else _snapshot_decimal(value, field_name)


class RealizedVolatility(TickIndicator):
    """Rolling population standard deviation of tick-to-tick simple returns.

    Every accepted trade after the first of a session contributes the simple
    return ``(price - previous) / previous``, stamped at the trade's
    timestamp.  At a bucket close as-of the exclusive end ``end``, returns in
    the half-open window ``[end - window, end)`` are retained and everything
    older is expired from the front of a deque with O(1) rolling sums.  A
    single retained return maps to ``0``; a bucket whose window holds no
    returns emits nothing.  Returns never span session boundaries: the
    rolling state and the previous price reset when a session rolls over.
    """

    NAME: ClassVar[str] = "realized_vol"
    CONSUMES: ClassVar[tuple[EventType, ...]] = (EventType.QUOTE, EventType.TRADE)
    DRIVERS: ClassVar[tuple[EventType, ...]] = (EventType.TRADE,)

    __slots__ = (
        "_count",
        "_entries",
        "_previous_price",
        "_sum",
        "_sum_squares",
        "_window",
    )

    def __init__(
        self,
        symbol: str | Symbol,
        *,
        window: timedelta | str,
        bucket: timedelta | str = "1min",
        session_anchor: time | None = MIDNIGHT_UTC,
    ) -> None:
        super().__init__(symbol, bucket=bucket, session_anchor=session_anchor)
        self._window = interval_delta(window)
        self._entries: deque[tuple[datetime, Decimal]] = deque()
        self._sum = _ZERO
        self._sum_squares = _ZERO
        self._count = 0
        self._previous_price: Decimal | None = None

    def _on_trade(self, trade: TradeEvent) -> None:
        price = trade.price
        previous = self._previous_price
        self._previous_price = price
        if previous is None:
            return
        entry = divide(subtract(price, previous), previous)
        self._entries.append((trade.timestamp, entry))
        self._sum = add(self._sum, entry)
        self._sum_squares = add(self._sum_squares, multiply(entry, entry))
        self._count += 1

    def _on_bucket_close(self, end: datetime) -> None:
        cutoff = end - self._window
        while self._entries and self._entries[0][0] < cutoff:
            _, expired = self._entries.popleft()
            self._sum = subtract(self._sum, expired)
            self._sum_squares = subtract(self._sum_squares, multiply(expired, expired))
            self._count -= 1

    def _reset_session(self) -> None:
        self._entries.clear()
        self._sum = _ZERO
        self._sum_squares = _ZERO
        self._count = 0
        self._previous_price = None

    def _compute_value(self) -> Decimal | None:
        if self._count == 0:
            return None
        divisor = Decimal(self._count)
        mean = divide(self._sum, divisor)
        variance = subtract(divide(self._sum_squares, divisor), multiply(mean, mean))
        if variance < 0:
            variance = _ZERO
        return sqrt(variance)

    @property
    def window(self) -> timedelta:
        """Return the exact rolling window duration."""
        return self._window

    def _indicator_params(self) -> dict[str, object]:
        return {"window": self._window // timedelta(microseconds=1)}

    def _snapshot_state(self) -> dict[str, object]:
        return {
            "count": self._count,
            "entries": [
                [moment.isoformat(), str(entry)] for moment, entry in self._entries
            ],
            "previous_price": (
                None if self._previous_price is None else str(self._previous_price)
            ),
            "sum": str(self._sum),
            "sum_squares": str(self._sum_squares),
        }

    def _restore_state(self, state: dict[str, object]) -> None:
        raw_entries = state.get("entries")
        if not isinstance(raw_entries, list):
            raise IndicatorSnapshotError("snapshot realized_vol entries must be a list")
        entries: deque[tuple[datetime, Decimal]] = deque()
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, list) or len(raw_entry) != 2:
                raise IndicatorSnapshotError(
                    "snapshot realized_vol entries must be [timestamp, return] pairs"
                )
            entries.append(
                (
                    _snapshot_datetime(raw_entry[0], "realized_vol entry timestamp"),
                    _snapshot_decimal(raw_entry[1], "realized_vol entry return"),
                )
            )
        count = _snapshot_int(state.get("count"), "realized_vol count")
        if count < 0 or count != len(entries):
            raise IndicatorSnapshotError("snapshot realized_vol count disagrees")
        self._entries = entries
        self._count = count
        self._sum = _snapshot_decimal(state.get("sum"), "realized_vol sum")
        self._sum_squares = _snapshot_decimal(
            state.get("sum_squares"), "realized_vol sum_squares"
        )
        raw_previous = state.get("previous_price")
        self._previous_price = (
            None
            if raw_previous is None
            else _snapshot_decimal(raw_previous, "realized_vol previous_price")
        )


__all__ = ["AverageTrueRange", "RealizedVolatility"]
