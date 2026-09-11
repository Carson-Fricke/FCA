"""Unadjusted versus adjusted divergence on a known split (T131 evidence).

This prototype is a deterministic, offline, dependency-free demonstration of
the realism gap tracked by the corporate-action spike in
``docs/post-v1-realism-spikes.md``: the harness ingests raw, as-traded prices
and models only delisting and rename lifecycle events
(``src/harness/data/listing_events.py``), so a split is invisible to bars,
returns, indicators, and position accounting.  The script contrasts the same
session window twice -- once as traded (unadjusted) and once with the
pre-split sessions divided by the split factor (adjusted) -- and shows that
the two produce contradictory one-day returns, window returns, and a moving
average crossover signal.

The window is Apple's 4-for-1 split, effective 2020-08-31.  The closes are
rounded vendor values embedded here so the prototype never touches the
network; the arithmetic is exact :class:`~decimal.Decimal`.

Run it directly to print the comparison table::

    uv run --no-sync python \
        scripts/realism/corporate_actions_adjusted_vs_unadjusted.py

It is also written as a focused pytest module (pass the file explicitly)::

    uv run --no-sync pytest scripts/realism/corporate_actions_adjusted_vs_unadjusted.py
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_SPLIT_SESSION = date(2020, 8, 31)
_SPLIT_FACTOR = Decimal("4")
_PERCENT = Decimal("0.01")
_SHORT_WINDOW = 3
_LONG_WINDOW = 5


@dataclass(frozen=True, slots=True)
class Bar:
    """One session close used by the prototype."""

    session: date
    close: Decimal


# As-traded (unadjusted) Apple closes spanning the 2020-08-31 4-for-1 split.
# Source values are rounded public vendor closes; they are fixed inputs so the
# prototype is deterministic and reproducible without network access.
UNADJUSTED: tuple[Bar, ...] = (
    Bar(date(2020, 8, 24), Decimal("503.43")),
    Bar(date(2020, 8, 25), Decimal("499.30")),
    Bar(date(2020, 8, 26), Decimal("506.09")),
    Bar(date(2020, 8, 27), Decimal("500.04")),
    Bar(date(2020, 8, 28), Decimal("499.23")),
    Bar(date(2020, 8, 31), Decimal("129.04")),
    Bar(date(2020, 9, 1), Decimal("134.18")),
    Bar(date(2020, 9, 2), Decimal("131.40")),
    Bar(date(2020, 9, 3), Decimal("120.88")),
    Bar(date(2020, 9, 4), Decimal("120.96")),
)


def adjusted(
    bars: Sequence[Bar],
    *,
    split_session: date,
    factor: Decimal,
) -> tuple[Bar, ...]:
    """Return the split-adjusted series with pre-split closes divided by ``factor``."""
    if factor <= 0:
        raise ValueError("split factor must be positive")
    return tuple(
        Bar(
            bar.session,
            bar.close / factor if bar.session < split_session else bar.close,
        )
        for bar in bars
    )


def daily_returns(bars: Sequence[Bar]) -> tuple[tuple[date, Decimal], ...]:
    """Return ``(session, simple_return)`` for every session after the first."""
    return tuple(
        (
            bars[index].session,
            (bars[index].close - bars[index - 1].close) / bars[index - 1].close,
        )
        for index in range(1, len(bars))
    )


def window_return(bars: Sequence[Bar]) -> Decimal:
    """Return the simple return from the first to the last close in ``bars``."""
    return (bars[-1].close - bars[0].close) / bars[0].close


def sma(bars: Sequence[Bar], window: int) -> Decimal | None:
    """Return the simple moving average of closes over the last ``window`` bars."""
    if len(bars) < window:
        return None
    tail = bars[-window:]
    return sum((bar.close for bar in tail), start=Decimal("0")) / Decimal(window)


def crossover_signal(bars: Sequence[Bar]) -> str:
    """Return the deterministic short/long SMA regime at the final session."""
    short = sma(bars, _SHORT_WINDOW)
    long = sma(bars, _LONG_WINDOW)
    if short is None or long is None:
        return "insufficient"
    if short > long:
        return "long"
    if short < long:
        return "flat"
    return "neutral"


def bars_through(bars: Sequence[Bar], session: date) -> tuple[Bar, ...]:
    """Return the window prefix ending at the first bar on or after ``session``."""
    for index, bar in enumerate(bars):
        if bar.session >= session:
            return tuple(bars[: index + 1])
    return tuple(bars)


def _split_day_return(bars: Sequence[Bar], *, split_session: date) -> Decimal:
    """Return the simple return printed on the split session."""
    for session, value in daily_returns(bars):
        if session == split_session:
            return value
    raise ValueError(f"{split_session.isoformat()} is outside the window")


def _format_percent(value: Decimal) -> str:
    return f"{(value / _PERCENT).quantize(Decimal('0.01')):+}%"


def main() -> None:
    """Print the unadjusted-versus-adjusted comparison and its findings."""
    adjusted_bars = adjusted(
        UNADJUSTED, split_session=_SPLIT_SESSION, factor=_SPLIT_FACTOR
    )
    raw_returns = dict(daily_returns(UNADJUSTED))
    adj_returns = dict(daily_returns(adjusted_bars))

    print("session       unadjusted    adjusted     raw return   adj return")
    for raw_bar, adj_bar in zip(UNADJUSTED, adjusted_bars, strict=True):
        raw = raw_returns.get(raw_bar.session)
        adj = adj_returns.get(adj_bar.session)
        raw_text = _format_percent(raw) if raw is not None else "        --"
        adj_text = _format_percent(adj) if adj is not None else "        --"
        print(
            f"{raw_bar.session.isoformat()}  {raw_bar.close:>9}  "
            f"{adj_bar.close:>9}  {raw_text:>11}  {adj_text:>10}"
        )

    raw_split = _split_day_return(UNADJUSTED, split_session=_SPLIT_SESSION)
    adj_split = _split_day_return(adjusted_bars, split_session=_SPLIT_SESSION)
    raw_window = window_return(UNADJUSTED)
    adj_window = window_return(adjusted_bars)
    raw_signal = crossover_signal(bars_through(UNADJUSTED, _SPLIT_SESSION))
    adj_signal = crossover_signal(bars_through(adjusted_bars, _SPLIT_SESSION))

    print()
    print(f"split session: {_SPLIT_SESSION.isoformat()} factor {_SPLIT_FACTOR}")
    print(
        "split-day return: "
        f"unadjusted {_format_percent(raw_split)} | "
        f"adjusted {_format_percent(adj_split)}"
    )
    print(
        "window return:    "
        f"unadjusted {_format_percent(raw_window)} | "
        f"adjusted {_format_percent(adj_window)}"
    )
    print(
        f"SMA{_SHORT_WINDOW}/SMA{_LONG_WINDOW} signal at split: {raw_signal} | {adj_signal}"
    )


def test_split_day_return_is_spurious_in_the_unadjusted_series() -> None:
    raw_split = _split_day_return(UNADJUSTED, split_session=_SPLIT_SESSION)
    adjusted_bars = adjusted(
        UNADJUSTED, split_session=_SPLIT_SESSION, factor=_SPLIT_FACTOR
    )
    adj_split = _split_day_return(adjusted_bars, split_session=_SPLIT_SESSION)

    assert raw_split < Decimal("-0.5")
    assert adj_split > Decimal("0")


def test_cumulative_returns_diverge_by_the_split_factor() -> None:
    adjusted_bars = adjusted(
        UNADJUSTED, split_session=_SPLIT_SESSION, factor=_SPLIT_FACTOR
    )

    divergence = abs(window_return(adjusted_bars) - window_return(UNADJUSTED))

    assert divergence > Decimal("0.5")


def test_moving_average_regime_flips_at_the_split() -> None:
    adjusted_bars = adjusted(
        UNADJUSTED, split_session=_SPLIT_SESSION, factor=_SPLIT_FACTOR
    )

    assert crossover_signal(bars_through(UNADJUSTED, _SPLIT_SESSION)) == "flat"
    assert crossover_signal(bars_through(adjusted_bars, _SPLIT_SESSION)) == "long"


def test_adjustment_is_deterministic() -> None:
    first = adjusted(UNADJUSTED, split_session=_SPLIT_SESSION, factor=_SPLIT_FACTOR)
    second = adjusted(UNADJUSTED, split_session=_SPLIT_SESSION, factor=_SPLIT_FACTOR)

    assert first == second


if __name__ == "__main__":
    main()
