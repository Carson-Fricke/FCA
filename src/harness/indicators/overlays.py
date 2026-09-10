"""Price-overlay indicators computed incrementally from tick events.

All three overlays keep O(1)-amortized state: rolling windows are maintained
as running totals over fixed-capacity deques, and the EMA is a single
recursive update per trade.  Emission, ordering, session, and snapshot
semantics come from :class:`~harness.indicators.base.TickIndicator`.
"""

from __future__ import annotations

from collections import deque
from datetime import time, timedelta
from decimal import Decimal
from typing import ClassVar, Final

from harness.core.symbols import Symbol
from harness.core.types.events import EventType, QuoteEvent, TradeEvent
from harness.indicators.base import (
    MIDNIGHT_UTC,
    IndicatorSnapshotError,
    TickIndicator,
    _positive_int,
    _snapshot_decimal,
    _snapshot_int,
    add,
    divide,
    multiply,
    subtract,
)

_TWO: Final = Decimal("2")
_ZERO: Final = Decimal("0")


class SimpleMovingAverage(TickIndicator):
    """Rolling mean of the last ``period`` trade prices.

    The window counts trades, not buckets: every accepted trade enters the
    rolling window and the oldest price leaves it, so the update is O(1)
    amortized and never recomputes over history.  Quotes are consumed (they
    can rotate buckets) but never contribute values.  The window is
    continuous across buckets and session boundaries, and a bucket emits a
    sample only once at least ``period`` trades have been accepted.
    """

    NAME: ClassVar[str] = "sma"
    CONSUMES: ClassVar[tuple[EventType, ...]] = (EventType.QUOTE, EventType.TRADE)
    DRIVERS: ClassVar[tuple[EventType, ...]] = (EventType.TRADE,)

    __slots__ = ("_period", "_total", "_window")

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
        self._window: deque[Decimal] = deque()
        self._total = _ZERO

    def _on_trade(self, trade: TradeEvent) -> None:
        self._window.append(trade.price)
        self._total = add(self._total, trade.price)
        if len(self._window) > self._period:
            self._total = subtract(self._total, self._window.popleft())

    def _compute_value(self) -> Decimal | None:
        if len(self._window) < self._period:
            return None
        return divide(self._total, Decimal(self._period))

    def _indicator_params(self) -> dict[str, object]:
        return {"period": self._period}

    def _snapshot_state(self) -> dict[str, object]:
        return {
            "total": str(self._total),
            "window": [str(price) for price in self._window],
        }

    def _restore_state(self, state: dict[str, object]) -> None:
        raw_window = state.get("window")
        if not isinstance(raw_window, list) or not all(
            isinstance(price, str) for price in raw_window
        ):
            raise IndicatorSnapshotError(
                "snapshot sma window must be a list of decimal strings"
            )
        if len(raw_window) > self._period:
            raise IndicatorSnapshotError(
                "snapshot sma window exceeds the configured period"
            )
        self._window = deque(
            _snapshot_decimal(price, "sma window price") for price in raw_window
        )
        self._total = _snapshot_decimal(state.get("total"), "sma total")


class ExponentialMovingAverage(TickIndicator):
    """Recursive EMA over trade prices, seeded on the first trade.

    ``alpha = 2 / (span + 1)`` is resolved once inside the pinned arithmetic
    context and every update performs one subtract, multiply, and add, so the
    recurrence is O(1) per trade and bit-identical across replays of the same
    event ordering.  The value warms up after ``span`` trades, and the series
    stays continuous across buckets and session boundaries.
    """

    NAME: ClassVar[str] = "ema"
    CONSUMES: ClassVar[tuple[EventType, ...]] = (EventType.QUOTE, EventType.TRADE)
    DRIVERS: ClassVar[tuple[EventType, ...]] = (EventType.TRADE,)

    __slots__ = ("_alpha", "_count", "_ema", "_span")

    def __init__(
        self,
        symbol: str | Symbol,
        *,
        span: int,
        bucket: timedelta | str = "1min",
        session_anchor: time | None = MIDNIGHT_UTC,
    ) -> None:
        super().__init__(symbol, bucket=bucket, session_anchor=session_anchor)
        self._span = _positive_int(span, "span")
        self._alpha = divide(_TWO, Decimal(span + 1))
        self._count = 0
        self._ema: Decimal | None = None

    def _on_trade(self, trade: TradeEvent) -> None:
        price = trade.price
        self._count += 1
        if self._ema is None:
            self._ema = price
        else:
            self._ema = add(
                self._ema, multiply(self._alpha, subtract(price, self._ema))
            )

    def _compute_value(self) -> Decimal | None:
        return None if self._count < self._span else self._ema

    def _indicator_params(self) -> dict[str, object]:
        return {"span": self._span}

    def _snapshot_state(self) -> dict[str, object]:
        return {
            "count": self._count,
            "ema": None if self._ema is None else str(self._ema),
        }

    def _restore_state(self, state: dict[str, object]) -> None:
        count = _snapshot_int(state.get("count"), "ema count")
        raw_ema = state.get("ema")
        if count < 0:
            raise IndicatorSnapshotError("snapshot ema count must be non-negative")
        self._count = count
        self._ema = None if raw_ema is None else _snapshot_decimal(raw_ema, "ema value")
        if (count > 0) != (self._ema is not None):
            raise IndicatorSnapshotError("snapshot ema count and value disagree")


class MidSimpleMovingAverage(TickIndicator):
    """Rolling mean of the last ``period`` quote mid-prices.

    The mid-price is the exact ``(bid + ask) / 2`` of each accepted quote.
    Quotes drive both the window and bucket rotation; trades are consumed for
    rotation only and never contribute values.  Warm-up, continuity, and
    emission follow the SMA contract.
    """

    NAME: ClassVar[str] = "mid_sma"
    CONSUMES: ClassVar[tuple[EventType, ...]] = (EventType.QUOTE, EventType.TRADE)
    DRIVERS: ClassVar[tuple[EventType, ...]] = (EventType.QUOTE,)

    __slots__ = ("_period", "_total", "_window")

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
        self._window: deque[Decimal] = deque()
        self._total = _ZERO

    def _on_quote(self, quote: QuoteEvent) -> None:
        mid = divide(add(quote.bid_price, quote.ask_price), _TWO)
        self._window.append(mid)
        self._total = add(self._total, mid)
        if len(self._window) > self._period:
            self._total = subtract(self._total, self._window.popleft())

    def _compute_value(self) -> Decimal | None:
        if len(self._window) < self._period:
            return None
        return divide(self._total, Decimal(self._period))

    def _indicator_params(self) -> dict[str, object]:
        return {"period": self._period}

    def _snapshot_state(self) -> dict[str, object]:
        return {
            "total": str(self._total),
            "window": [str(mid) for mid in self._window],
        }

    def _restore_state(self, state: dict[str, object]) -> None:
        raw_window = state.get("window")
        if not isinstance(raw_window, list) or not all(
            isinstance(mid, str) for mid in raw_window
        ):
            raise IndicatorSnapshotError(
                "snapshot mid_sma window must be a list of decimal strings"
            )
        if len(raw_window) > self._period:
            raise IndicatorSnapshotError(
                "snapshot mid_sma window exceeds the configured period"
            )
        self._window = deque(
            _snapshot_decimal(mid, "mid_sma window mid") for mid in raw_window
        )
        self._total = _snapshot_decimal(state.get("total"), "mid_sma total")


__all__ = [
    "ExponentialMovingAverage",
    "MidSimpleMovingAverage",
    "SimpleMovingAverage",
]
