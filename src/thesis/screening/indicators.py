"""Bridge from daily OHLCV bars to harness tick-native indicators.

The screener's technical filters are computed through the published harness
indicator registry (``create_indicator(name, params, symbol=...)``), never
hand-rolled.  Registry indicators are tick-native, so this module encodes one
symbol's daily bars into deterministic synthetic ``TradeEvent`` streams and
replays them through a fresh indicator instance:

- close-series indicators (``sma``, ``ema``, ``rsi``) receive one trade per
  bar at the bar close with the bar's volume;
- the ``atr`` range indicator receives three trades per bar (high, low,
  close) inside the bar's UTC day with a one-day bucket, so each bucket's
  high/low/close are exactly the bar's values and true ranges are exact;
- ``dollar_volume`` receives one trade per bar so its notional is the exact
  ``close * volume`` sum over the fed window.

Event times are fixed UTC hours of the bar date and the replay runs with
``session_anchor=None`` (sessions off), so the same bars always produce the
same value.  Bars stamped after the requested window end are clipped before
the replay, which keeps the computation point-in-time safe even when a port
returns extra history.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Final, Protocol

from harness.core.symbols import canonical_symbol
from harness.core.types.events import TradeEvent
from harness.indicators import TickIndicator, create_indicator

_CLOSE_TIME: Final = time(20, 0)
_HIGH_TIME: Final = time(13, 0)
_LOW_TIME: Final = time(14, 0)
_RANGE_INDICATORS: Final[frozenset[str]] = frozenset({"atr"})
_WARMUP_PARAM_BY_NAME: Final[Mapping[str, str]] = {
    "sma": "period",
    "mid_sma": "period",
    "rsi": "period",
    "atr": "period",
    "ema": "span",
}
_MAX_WARMUP_DAYS: Final = 500


class IndicatorBridgeError(RuntimeError):
    """A daily-bar window could not be replayed through an indicator."""


@dataclass(frozen=True, slots=True)
class OhlcvBar:
    """One daily OHLCV bar with exact decimal values."""

    bar_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.bar_date, datetime):
            raise TypeError("bar_date must be a date, not a datetime")
        if not isinstance(self.bar_date, date):
            raise TypeError("bar_date must be a date")
        for field_name in ("open", "high", "low", "close", "volume"):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{field_name} must be a Decimal")
        if self.volume < 0:
            raise ValueError("volume must not be negative")
        if (
            self.high < self.low
            or self.open < self.low
            or self.open > self.high
            or self.close < self.low
            or self.close > self.high
        ):
            raise ValueError("OHLC values must reconcile within low and high")

    @property
    def dollar_volume(self) -> Decimal:
        """Return the bar's traded notional."""
        return self.close * self.volume


class OhlcvPort(Protocol):
    """The injected daily-bar surface the screener context reads."""

    def daily_bars(self, symbol: str, start: date, end: date) -> Sequence[OhlcvBar]:
        """Return daily bars for ``symbol`` in the inclusive date window."""
        ...


def indicator_warmup_days(name: str, params: Mapping[str, object]) -> int:
    """Return the extra history (in days) an indicator needs before its window.

    Period-based indicators need one trade per period plus the change seed;
    windowed indicators need their duration; everything else warms up within
    the screening window itself.
    """
    period_param = _WARMUP_PARAM_BY_NAME.get(name)
    if period_param is not None:
        period = params.get(period_param)
        if isinstance(period, bool) or not isinstance(period, int) or period < 1:
            raise IndicatorBridgeError(
                f"indicator {name!r} requires a positive integer {period_param!r}"
            )
        return period + 1
    window = params.get("window")
    if isinstance(window, str) and window.strip().lower().endswith("d"):
        try:
            return int(window.strip().lower()[:-1])
        except ValueError as error:
            raise IndicatorBridgeError(
                f"indicator {name!r} window {window!r} is not a day duration"
            ) from error
    return 0


def clip_bars(
    bars: Sequence[OhlcvBar], *, window_start: date, window_end: date
) -> tuple[OhlcvBar, ...]:
    """Return the bars inside the inclusive window, sorted by bar date.

    A bar stamped after ``window_end`` is invisible no matter what the port
    returned; duplicate or out-of-order dates are rejected so the replay
    stays deterministic.
    """
    visible = [bar for bar in bars if window_start <= bar.bar_date <= window_end]
    visible.sort(key=lambda bar: bar.bar_date)
    for earlier, later in zip(visible, visible[1:], strict=False):
        if earlier.bar_date == later.bar_date:
            raise IndicatorBridgeError(
                f"duplicate bar for {later.bar_date.isoformat()}"
            )
    return tuple(visible)


def _trade_quantity(volume: Decimal) -> Decimal:
    if volume <= 0:
        raise IndicatorBridgeError("daily bar volume must be positive")
    return volume


def _close_series_events(
    symbol: str, bars: Sequence[OhlcvBar]
) -> tuple[TradeEvent, ...]:
    canonical = canonical_symbol(symbol)
    return tuple(
        TradeEvent(
            event_id=f"synthetic-{symbol}-{bar.bar_date.isoformat()}-close",
            timestamp=datetime.combine(bar.bar_date, _CLOSE_TIME, tzinfo=UTC),
            sequence=index,
            symbol=canonical,
            price=bar.close,
            quantity=_trade_quantity(bar.volume),
        )
        for index, bar in enumerate(bars)
    )


def _bar_range_events(symbol: str, bars: Sequence[OhlcvBar]) -> tuple[TradeEvent, ...]:
    canonical = canonical_symbol(symbol)
    events: list[TradeEvent] = []
    sequence = 0
    for bar in bars:
        day = bar.bar_date
        for moment, price in (
            (_HIGH_TIME, bar.high),
            (_LOW_TIME, bar.low),
            (_CLOSE_TIME, bar.close),
        ):
            events.append(
                TradeEvent(
                    event_id=f"synthetic-{symbol}-{day.isoformat()}-{sequence}",
                    timestamp=datetime.combine(day, moment, tzinfo=UTC),
                    sequence=sequence,
                    symbol=canonical,
                    price=price,
                    quantity=Decimal("1"),
                )
            )
            sequence += 1
    return tuple(events)


def compute_indicator_value(
    name: str,
    params: Mapping[str, object],
    symbol: str,
    bars: Sequence[OhlcvBar],
    *,
    window_start: date,
    window_end: date,
    indicator: TickIndicator | None = None,
) -> Decimal | None:
    """Compute one indicator's value at ``window_end`` over daily bars.

    The bars are clipped to the window (plus any warm-up history the caller
    already included in ``window_start``), replayed through a fresh
    registry-built indicator on one-day buckets with sessions off, flushed,
    and read.  ``None`` means the indicator never warmed up inside the
    supplied bars.
    """
    from harness.indicators import IndicatorOrderingError

    visible = clip_bars(bars, window_start=window_start, window_end=window_end)
    if not visible:
        return None
    if name in _RANGE_INDICATORS:
        events = _bar_range_events(symbol, visible)
    else:
        events = _close_series_events(symbol, visible)
    creation = dict(params)
    creation.setdefault("bucket", "1d")
    instance = indicator or create_indicator(
        name, creation, symbol=symbol, session_anchor=None
    )
    try:
        for event in events:
            instance.on_event(event)
        instance.flush()
    except IndicatorOrderingError as error:
        raise IndicatorBridgeError(
            f"daily bars for {symbol} are not in deterministic order: {error}"
        ) from error
    return instance.value


def indicator_fetch_start(
    name: str,
    params: Mapping[str, object],
    *,
    window_start: date,
) -> date:
    """Return the fetch start that gives an indicator its warm-up history."""
    warmup = min(indicator_warmup_days(name, params), _MAX_WARMUP_DAYS)
    return window_start - timedelta(days=warmup)


__all__ = [
    "IndicatorBridgeError",
    "OhlcvBar",
    "OhlcvPort",
    "clip_bars",
    "compute_indicator_value",
    "indicator_fetch_start",
    "indicator_warmup_days",
]
