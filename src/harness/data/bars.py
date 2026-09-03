"""Exact OHLCV bar derivation from immutable trade events."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.common import timestamp
from harness.core.types.events import TradeEvent

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_ZERO: Final = Decimal("0")


@dataclass(frozen=True, slots=True)
class OHLCVBar:
    """One non-empty, half-open OHLCV interval derived from trade events.

    ``start`` is inclusive and ``end`` is exclusive.  Prices and volume remain
    ``Decimal`` values all the way through aggregation so the result can be
    reconciled exactly to its source trades.
    """

    symbol: Symbol
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        start_time = timestamp(self.start, "start")
        end_time = timestamp(self.end, "end")
        if end_time <= start_time:
            raise ValueError("end must be after start")
        object.__setattr__(self, "start", start_time)
        object.__setattr__(self, "end", end_time)
        for field_name in ("open", "high", "low", "close", "volume"):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{field_name} must be a Decimal")
            if not value.is_finite():
                raise ValueError(f"{field_name} must be finite")
        if self.volume < _ZERO:
            raise ValueError("volume must not be negative")
        if (
            self.high < self.low
            or self.open < self.low
            or self.open > self.high
            or self.close < self.low
            or self.close > self.high
        ):
            raise ValueError("OHLC values must reconcile within low and high")
        if (
            isinstance(self.trade_count, bool)
            or not isinstance(self.trade_count, int)
            or self.trade_count <= 0
        ):
            raise ValueError("trade_count must be a positive integer")

    @property
    def timestamp(self) -> datetime:
        """Return the bar start under the conventional OHLCV name."""
        return self.start

    @property
    def open_time(self) -> datetime:
        """Return the inclusive bar start."""
        return self.start

    @property
    def start_time(self) -> datetime:
        """Return the inclusive bar start under the session contract name."""
        return self.start

    @property
    def close_time(self) -> datetime:
        """Return the exclusive bar end."""
        return self.end

    @property
    def end_time(self) -> datetime:
        """Return the exclusive bar end under the session contract name."""
        return self.end

    @property
    def count(self) -> int:
        """Return the number of source trades in this bar."""
        return self.trade_count


Bar = OHLCVBar
OHLCV = OHLCVBar


def interval_delta(interval: timedelta | str) -> timedelta:
    """Normalize a configured bar interval to a positive duration.

    Strings use the harness configuration vocabulary: ``s``, ``min``, ``h``,
    and ``d``.  The familiar ``m`` spelling is accepted as an input alias but
    is never emitted or used as a separate internal unit.
    """
    if isinstance(interval, timedelta):
        result = interval
    elif isinstance(interval, str):
        value = interval.strip().lower()
        number = value.rstrip("abcdefghijklmnopqrstuvwxyz")
        unit = value[len(number) :]
        if not number or unit not in {"s", "m", "min", "h", "d"}:
            raise ValueError("interval must be a positive timedelta or s/min/h/d value")
        try:
            amount = int(number)
        except ValueError as error:
            raise ValueError(
                "interval must be a positive timedelta or s/min/h/d value"
            ) from error
        if amount <= 0:
            raise ValueError("interval must be positive")
        if unit == "s":
            result = timedelta(seconds=amount)
        elif unit in {"m", "min"}:
            result = timedelta(minutes=amount)
        elif unit == "h":
            result = timedelta(hours=amount)
        else:
            result = timedelta(days=amount)
    else:
        raise TypeError("interval must be a timedelta or string")
    if result <= timedelta(0):
        raise ValueError("interval must be positive")
    return result


def _bucket_start(value: datetime, interval: timedelta) -> datetime:
    elapsed = value - _EPOCH
    periods = elapsed // interval
    return _EPOCH + periods * interval


class BarAggregator:
    """Accumulate one symbol's trades into deterministic OHLCV bars."""

    __slots__ = ("_interval", "_symbol", "_buckets")

    def __init__(
        self, interval: timedelta | str, *, symbol: str | Symbol | None = None
    ) -> None:
        self._interval = interval_delta(interval)
        self._symbol = None if symbol is None else canonical_symbol(symbol)
        self._buckets: dict[datetime, list[TradeEvent]] = {}

    @property
    def interval(self) -> timedelta:
        """Return the exact configured interval."""
        return self._interval

    @property
    def symbol(self) -> Symbol | None:
        """Return the selected symbol, if one was configured."""
        return self._symbol

    def add(self, trade: TradeEvent) -> None:
        """Add one trade without consulting any external clock or data source."""
        if not isinstance(trade, TradeEvent):
            raise TypeError("trade must be a TradeEvent")
        if self._symbol is None:
            self._symbol = trade.symbol
        elif trade.symbol != self._symbol:
            raise ValueError("all trades must have the same symbol")
        bucket = _bucket_start(trade.timestamp, self._interval)
        self._buckets.setdefault(bucket, []).append(trade)

    def add_many(self, trades: Iterable[TradeEvent]) -> None:
        """Add trades in source-feed order."""
        for trade in trades:
            self.add(trade)

    def finalize(self) -> tuple[OHLCVBar, ...]:
        """Return non-empty bars ordered by UTC interval start."""
        return tuple(
            _make_bar(start, self._interval, trades)
            for start, trades in sorted(self._buckets.items())
        )

    def aggregate(self, trades: Iterable[TradeEvent]) -> tuple[OHLCVBar, ...]:
        """Add ``trades`` and return the resulting bars."""
        self.add_many(trades)
        return self.finalize()


BarBuilder = BarAggregator


def _make_bar(
    start: datetime, interval: timedelta, trades: list[TradeEvent]
) -> OHLCVBar:
    if not trades:
        raise ValueError("cannot build a bar without trades")
    ordered = sorted(
        enumerate(trades),
        key=lambda item: (item[1].timestamp, item[1].sequence, item[0]),
    )
    source = [trade for _, trade in ordered]
    prices = tuple(trade.price for trade in source)
    return OHLCVBar(
        symbol=source[0].symbol,
        start=start,
        end=start + interval,
        open=prices[0],
        high=max(prices),
        low=min(prices),
        close=prices[-1],
        volume=sum((trade.quantity for trade in source), start=_ZERO),
        trade_count=len(source),
    )


def build_bars(
    trades: Iterable[TradeEvent],
    interval: timedelta | str,
    *,
    symbol: str | Symbol | None = None,
) -> tuple[OHLCVBar, ...]:
    """Build exact bars from a finite trade iterable."""
    return BarAggregator(interval, symbol=symbol).aggregate(trades)


aggregate_trades = build_bars
bars_from_trades = build_bars


__all__ = [
    "Bar",
    "BarAggregator",
    "BarBuilder",
    "OHLCV",
    "OHLCVBar",
    "aggregate_trades",
    "bars_from_trades",
    "build_bars",
    "interval_delta",
]
