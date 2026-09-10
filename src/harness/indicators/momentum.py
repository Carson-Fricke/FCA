"""Momentum indicators computed incrementally from tick events.

Emission, ordering, session, and snapshot semantics come from
:class:`~harness.indicators.base.TickIndicator`.
"""

from __future__ import annotations

from datetime import time, timedelta
from decimal import Decimal
from typing import ClassVar, Final

from harness.core.symbols import Symbol
from harness.core.types.events import EventType, TradeEvent
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

_HUNDRED: Final = Decimal("100")
_ZERO: Final = Decimal("0")


class RelativeStrengthIndex(TickIndicator):
    """Wilder RSI over consecutive trade-price changes.

    Each accepted trade after the first contributes the price change against
    the previous trade, split into gain and loss.  The first ``period``
    changes seed simple average gain and loss; every later change applies
    Wilder smoothing with ``alpha = 1 / period``, so the update is O(1) and
    the value warms up after ``period + 1`` trades.  The value is
    ``100 * avg_gain / (avg_gain + avg_loss)`` inside the pinned arithmetic
    context; a completely flat stream maps to ``100``.  The series is
    continuous across buckets and session boundaries.
    """

    NAME: ClassVar[str] = "rsi"
    CONSUMES: ClassVar[tuple[EventType, ...]] = (EventType.QUOTE, EventType.TRADE)
    DRIVERS: ClassVar[tuple[EventType, ...]] = (EventType.TRADE,)

    __slots__ = (
        "_avg_gain",
        "_avg_loss",
        "_changes",
        "_gain_total",
        "_loss_total",
        "_period",
        "_previous_price",
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
        self._previous_price: Decimal | None = None
        self._changes = 0
        self._gain_total = _ZERO
        self._loss_total = _ZERO
        self._avg_gain: Decimal | None = None
        self._avg_loss: Decimal | None = None

    def _on_trade(self, trade: TradeEvent) -> None:
        price = trade.price
        previous = self._previous_price
        self._previous_price = price
        if previous is None:
            return
        change = subtract(price, previous)
        gain = change if change > 0 else _ZERO
        loss = -change if change < 0 else _ZERO
        if self._avg_gain is None or self._avg_loss is None:
            self._gain_total = add(self._gain_total, gain)
            self._loss_total = add(self._loss_total, loss)
            self._changes += 1
            if self._changes == self._period:
                divisor = Decimal(self._period)
                self._avg_gain = divide(self._gain_total, divisor)
                self._avg_loss = divide(self._loss_total, divisor)
        else:
            divisor = Decimal(self._period)
            self._avg_gain = add(
                self._avg_gain, divide(subtract(gain, self._avg_gain), divisor)
            )
            self._avg_loss = add(
                self._avg_loss, divide(subtract(loss, self._avg_loss), divisor)
            )

    def _compute_value(self) -> Decimal | None:
        if self._avg_gain is None or self._avg_loss is None:
            return None
        total = add(self._avg_gain, self._avg_loss)
        if total == 0:
            return _HUNDRED
        return divide(multiply(_HUNDRED, self._avg_gain), total)

    def _indicator_params(self) -> dict[str, object]:
        return {"period": self._period}

    def _snapshot_state(self) -> dict[str, object]:
        return {
            "avg_gain": None if self._avg_gain is None else str(self._avg_gain),
            "avg_loss": None if self._avg_loss is None else str(self._avg_loss),
            "changes": self._changes,
            "gain_total": str(self._gain_total),
            "loss_total": str(self._loss_total),
            "previous_price": (
                None if self._previous_price is None else str(self._previous_price)
            ),
        }

    def _restore_state(self, state: dict[str, object]) -> None:
        raw_previous = state.get("previous_price")
        raw_avg_gain = state.get("avg_gain")
        raw_avg_loss = state.get("avg_loss")
        previous: Decimal | None = None
        if raw_previous is not None:
            previous = _snapshot_decimal(raw_previous, "rsi previous_price")
        avg_gain: Decimal | None = None
        if raw_avg_gain is not None:
            avg_gain = _snapshot_decimal(raw_avg_gain, "rsi avg_gain")
        avg_loss: Decimal | None = None
        if raw_avg_loss is not None:
            avg_loss = _snapshot_decimal(raw_avg_loss, "rsi avg_loss")
        changes = _snapshot_int(state.get("changes"), "rsi changes")
        if changes < 0 or changes > self._period:
            raise IndicatorSnapshotError("snapshot rsi changes is out of range")
        if (avg_gain is None) != (avg_loss is None):
            raise IndicatorSnapshotError("snapshot rsi averages disagree")
        if previous is None:
            if changes != 0 or avg_gain is not None:
                raise IndicatorSnapshotError(
                    "snapshot rsi warm-up state without a previous price"
                )
        elif (avg_gain is None) != (changes < self._period):
            raise IndicatorSnapshotError(
                "snapshot rsi averages disagree with warm-up progress"
            )
        self._previous_price = previous
        self._changes = changes
        self._gain_total = _snapshot_decimal(state.get("gain_total"), "rsi gain_total")
        self._loss_total = _snapshot_decimal(state.get("loss_total"), "rsi loss_total")
        self._avg_gain = avg_gain
        self._avg_loss = avg_loss


__all__ = ["RelativeStrengthIndex"]
