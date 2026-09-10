"""Volume-based indicators computed incrementally from tick events.

Emission, ordering, session, and snapshot semantics come from
:class:`~harness.indicators.base.TickIndicator`.  The cumulative members of
this module (session VWAP, cumulative dollar volume, on-balance volume)
reset their per-session state on every session rollover.
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
    _snapshot_datetime,
    _snapshot_decimal,
    _snapshot_int,
    add,
    divide,
    multiply,
    subtract,
)

_ZERO: Final = Decimal("0")


class VolumeWeightedAveragePrice(TickIndicator):
    """Session-cumulative volume-weighted average price of trades.

    Notional and volume accumulate exactly over accepted trades; the value is
    ``notional / volume`` inside the pinned arithmetic context and warms up
    with the first trade of the session.  Both accumulators reset when a
    session rolls over, so the emitted series is the classic session VWAP.
    """

    NAME: ClassVar[str] = "vwap"
    CONSUMES: ClassVar[tuple[EventType, ...]] = (EventType.QUOTE, EventType.TRADE)
    DRIVERS: ClassVar[tuple[EventType, ...]] = (EventType.TRADE,)

    __slots__ = ("_notional", "_volume")

    def __init__(
        self,
        symbol: str | Symbol,
        *,
        bucket: timedelta | str = "1min",
        session_anchor: time | None = MIDNIGHT_UTC,
    ) -> None:
        super().__init__(symbol, bucket=bucket, session_anchor=session_anchor)
        self._notional = _ZERO
        self._volume = _ZERO

    def _on_trade(self, trade: TradeEvent) -> None:
        self._notional = add(self._notional, multiply(trade.price, trade.quantity))
        self._volume = add(self._volume, trade.quantity)

    def _reset_session(self) -> None:
        self._notional = _ZERO
        self._volume = _ZERO

    def _compute_value(self) -> Decimal | None:
        if self._volume == 0:
            return None
        return divide(self._notional, self._volume)

    def _snapshot_state(self) -> dict[str, object]:
        return {"notional": str(self._notional), "volume": str(self._volume)}

    def _restore_state(self, state: dict[str, object]) -> None:
        self._notional = _snapshot_decimal(state.get("notional"), "vwap notional")
        self._volume = _snapshot_decimal(state.get("volume"), "vwap volume")


class DollarVolume(TickIndicator):
    """Traded notional, cumulative per session or rolling over a duration.

    With ``window=None`` the indicator is the session-cumulative sum of
    ``price * quantity`` over accepted trades, resetting on session rollover.
    With a duration window the indicator keeps the notional of trades stamped
    in the half-open window ``[end - window, end)`` at every bucket close as
    of ``end``, expiring older trades from a deque with an O(1) running sum;
    the rolling series also resets on session rollover.  A rolling value of
    an exhausted window is ``0`` rather than undefined.
    """

    NAME: ClassVar[str] = "dollar_volume"
    CONSUMES: ClassVar[tuple[EventType, ...]] = (EventType.QUOTE, EventType.TRADE)
    DRIVERS: ClassVar[tuple[EventType, ...]] = (EventType.TRADE,)

    __slots__ = ("_entries", "_notional", "_window")

    def __init__(
        self,
        symbol: str | Symbol,
        *,
        window: timedelta | str | None = None,
        bucket: timedelta | str = "1min",
        session_anchor: time | None = MIDNIGHT_UTC,
    ) -> None:
        super().__init__(symbol, bucket=bucket, session_anchor=session_anchor)
        self._window = None if window is None else interval_delta(window)
        self._entries: deque[tuple[datetime, Decimal]] = deque()
        self._notional = _ZERO

    @property
    def window(self) -> timedelta | None:
        """Return the rolling window, or ``None`` in cumulative mode."""
        return self._window

    def _on_trade(self, trade: TradeEvent) -> None:
        notional = multiply(trade.price, trade.quantity)
        if self._window is not None:
            self._entries.append((trade.timestamp, notional))
        self._notional = add(self._notional, notional)

    def _on_bucket_close(self, end: datetime) -> None:
        if self._window is None:
            return
        cutoff = end - self._window
        while self._entries and self._entries[0][0] < cutoff:
            self._notional = subtract(self._notional, self._entries.popleft()[1])

    def _reset_session(self) -> None:
        self._entries.clear()
        self._notional = _ZERO

    def _compute_value(self) -> Decimal | None:
        return self._notional

    def _indicator_params(self) -> dict[str, object]:
        if self._window is None:
            return {"window": None}
        return {"window": self._window // timedelta(microseconds=1)}

    def _snapshot_state(self) -> dict[str, object]:
        return {
            "entries": [
                [moment.isoformat(), str(notional)]
                for moment, notional in self._entries
            ],
            "notional": str(self._notional),
        }

    def _restore_state(self, state: dict[str, object]) -> None:
        raw_entries = state.get("entries")
        if not isinstance(raw_entries, list):
            raise IndicatorSnapshotError(
                "snapshot dollar_volume entries must be a list"
            )
        entries: deque[tuple[datetime, Decimal]] = deque()
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, list) or len(raw_entry) != 2:
                raise IndicatorSnapshotError(
                    "snapshot dollar_volume entries must be [timestamp, notional] pairs"
                )
            entries.append(
                (
                    _snapshot_datetime(raw_entry[0], "dollar_volume entry timestamp"),
                    _snapshot_decimal(raw_entry[1], "dollar_volume entry notional"),
                )
            )
        self._entries = entries
        self._notional = _snapshot_decimal(
            state.get("notional"), "dollar_volume notional"
        )


class OnBalanceVolume(TickIndicator):
    """On-balance volume from the tick rule, cumulative per session.

    Each accepted trade after the first of a session is signed against the
    previous trade price: an uptick adds its quantity, a downtick subtracts
    it, and a flat trade repeats the previous sign (the classic tick rule);
    the session's first trade contributes ``0``.  The running total resets
    when a session rolls over.
    """

    NAME: ClassVar[str] = "obv"
    CONSUMES: ClassVar[tuple[EventType, ...]] = (EventType.QUOTE, EventType.TRADE)
    DRIVERS: ClassVar[tuple[EventType, ...]] = (EventType.TRADE,)

    __slots__ = ("_last_sign", "_obv", "_previous_price")

    def __init__(
        self,
        symbol: str | Symbol,
        *,
        bucket: timedelta | str = "1min",
        session_anchor: time | None = MIDNIGHT_UTC,
    ) -> None:
        super().__init__(symbol, bucket=bucket, session_anchor=session_anchor)
        self._obv = _ZERO
        self._previous_price: Decimal | None = None
        self._last_sign = 0

    def _on_trade(self, trade: TradeEvent) -> None:
        price = trade.price
        previous = self._previous_price
        self._previous_price = price
        if previous is None:
            sign = 0
        elif price > previous:
            sign = 1
        elif price < previous:
            sign = -1
        else:
            sign = self._last_sign
        self._last_sign = sign
        if sign > 0:
            self._obv = add(self._obv, trade.quantity)
        elif sign < 0:
            self._obv = subtract(self._obv, trade.quantity)

    def _reset_session(self) -> None:
        self._obv = _ZERO
        self._previous_price = None
        self._last_sign = 0

    def _compute_value(self) -> Decimal | None:
        return self._obv

    def _snapshot_state(self) -> dict[str, object]:
        return {
            "last_sign": self._last_sign,
            "obv": str(self._obv),
            "previous_price": (
                None if self._previous_price is None else str(self._previous_price)
            ),
        }

    def _restore_state(self, state: dict[str, object]) -> None:
        sign = _snapshot_int(state.get("last_sign"), "obv last_sign")
        if sign not in (-1, 0, 1):
            raise IndicatorSnapshotError("snapshot obv last_sign is out of range")
        self._last_sign = sign
        self._obv = _snapshot_decimal(state.get("obv"), "obv value")
        raw_previous = state.get("previous_price")
        self._previous_price = (
            None
            if raw_previous is None
            else _snapshot_decimal(raw_previous, "obv previous_price")
        )


__all__ = [
    "DollarVolume",
    "OnBalanceVolume",
    "VolumeWeightedAveragePrice",
]
