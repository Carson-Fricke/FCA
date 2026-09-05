"""Offline static HTML report for one recorded run.

This module renders one run's artifact data as a single self-contained
HTML document: inline CSS, hand-rendered inline SVG charts, and no
JavaScript, web fonts, images, or any other external or network asset,
so the document renders inside a sandbox with no network access.

The renderer is a pure mapping from artifact data to an HTML string.  It
performs no I/O, reads no wall clock, draws no randomness, and touches no
network: nothing in the document depends on when it was rendered, so the
same artifacts always produce byte-identical HTML.

Conventions
-----------

Determinism
    The document carries no wall-clock timestamp.  Timelines are ordered
    by timestamp then identifier, slippage aggregates are computed over
    canonically sorted samples, and every mapping is rendered in sorted
    key order, so equivalent inputs render identically regardless of
    insertion order.

Downsampling
    Charts draw at most ``max_chart_points`` samples and tables list at
    most ``max_table_rows`` rows, both selected by an integer stride that
    keeps the first and last entries; omitted counts are stated in the
    document.  A five-year daily report therefore stays far below five
    megabytes.  Exact aggregates (total return, max drawdown, slippage
    totals) are always computed from the full data, never from the
    downsampled views.

Money and floats
    Monetary and ratio arithmetic uses :class:`decimal.Decimal` with the
    string-exact encoding conventions of :mod:`harness.results.metrics`;
    floating point appears only in final SVG pixel coordinates and in
    presentation rounding of ratio labels.  Equity-curve aggregates reuse
    :func:`harness.results.metrics.equity_curve_metrics` unchanged.

Data range
    The manifest header shows the header's own data range when supplied
    and otherwise derives one from the first and last equity sample.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Final

from harness.core.types.common import SCHEMA_VERSION, exact_decimal, text
from harness.results.metrics import EquityPoint, FillRow, equity_curve_metrics
from harness.results.recorder.artifacts import artifact_path, read_rows
from harness.results.recorder.schemas import Artifact

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NS_PER_DAY: Final = 86_400_000_000_000
_NS_PER_HOUR: Final = 3_600_000_000_000
_NS_PER_MINUTE: Final = 60_000_000_000
_DEFAULT_CHART_POINTS: Final = 1024
_DEFAULT_TABLE_ROWS: Final = 200
_DEFAULT_WIDTH: Final = 960
_HISTOGRAM_BINS: Final = 20
_MIN_WIDTH: Final = 320
_MAX_WIDTH: Final = 4096
_MISSING: Final = "\u2014"
_EMPTY_METADATA: Final[Mapping[str, str]] = MappingProxyType({})

_CSS: Final = """
:root { color-scheme: light; }
body { margin: 0; padding: 2rem; background: #f8fafc; color: #1f2937;
       font-family: system-ui, "Segoe UI", sans-serif; }
main { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 1.35rem; margin: 0 0 1rem; }
h2 { font-size: 1.05rem; margin: 0 0 0.9rem; color: #0f172a; }
section { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 8px;
          padding: 1.25rem; margin: 0 0 1.25rem; }
nav { margin: 0 0 1rem; font-size: 0.85rem; }
nav a { margin-right: 0.75rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
th, td { text-align: left; padding: 0.3rem 0.5rem;
         border-bottom: 1px solid #e5e7eb; vertical-align: top; }
th { color: #64748b; font-weight: 600; white-space: nowrap; }
code { font-family: ui-monospace, Consolas, monospace; font-size: 0.85em;
       overflow-wrap: anywhere; }
.chart { width: 100%; height: auto; }
.label { font-size: 11px; fill: #64748b; }
.title { font-size: 12px; font-weight: 600; fill: #334155; }
.grid { stroke: #e2e8f0; stroke-width: 1; }
.line-equity { fill: none; stroke: #2563eb; stroke-width: 2; }
.dot-equity { fill: #2563eb; }
.area-dd { fill: rgba(220, 38, 38, 0.25); stroke: #dc2626; stroke-width: 1; }
.zero { stroke: #64748b; stroke-width: 1; stroke-dasharray: 4 3; }
.bar { fill: #2563eb; }
circle.win { fill: #16a34a; }
circle.loss { fill: #dc2626; }
circle.flat { fill: #64748b; }
circle.tick { fill: #64748b; }
circle.tick.fill { fill: #2563eb; }
circle.tick.close { fill: #ea580c; }
circle.tick.mark { fill: #94a3b8; }
circle.tick.snapshot { fill: #7c3aed; }
.metrics { margin: 0 0 0.9rem; }
.metric { margin-right: 1.1rem; white-space: nowrap; }
.muted { color: #64748b; }
.note { color: #64748b; font-size: 0.8rem; }
table.kv td:first-child { color: #475569; width: 15rem; }
footer { color: #64748b; font-size: 0.8rem; }
"""

_NAV: Final[tuple[tuple[str, str], ...]] = (
    ("manifest", "Manifest"),
    ("equity", "Equity and drawdown"),
    ("trades", "Trades"),
    ("positions", "Positions"),
    ("risk", "Risk events"),
    ("slippage", "Slippage"),
)


@dataclass(frozen=True, slots=True)
class ReportHeader:
    """Manifest header facts rendered at the top of every report."""

    run_id: str
    engine: str = ""
    config_hash: str = ""
    code_fingerprint: str = ""
    seed: str = ""
    data_start: date | None = None
    data_end: date | None = None
    metadata: Mapping[str, str] = _EMPTY_METADATA

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", str(text(self.run_id, "run_id")))
        for name in ("engine", "config_hash", "code_fingerprint", "seed"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
        if isinstance(self.data_start, datetime):
            raise TypeError("data_start must be a date, not a datetime")
        if isinstance(self.data_end, datetime):
            raise TypeError("data_end must be a date, not a datetime")
        if (
            self.data_start is not None
            and self.data_end is not None
            and self.data_start > self.data_end
        ):
            raise ValueError("data_start must be on or before data_end")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(
                {str(key): str(value) for key, value in self.metadata.items()}
            ),
        )


@dataclass(frozen=True, slots=True)
class ReportTrade:
    """One realized trade event shown on the trade timeline."""

    trade_id: str
    realized_pnl: Decimal
    strategy_id: str = ""
    symbol: str | None = None
    opened_ns: int | None = None
    closed_ns: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trade_id", str(text(self.trade_id, "trade_id")))
        object.__setattr__(
            self,
            "realized_pnl",
            exact_decimal(self.realized_pnl, "realized_pnl"),
        )
        if not isinstance(self.strategy_id, str):
            raise TypeError("strategy_id must be a string")
        if self.symbol is not None and not isinstance(self.symbol, str):
            raise TypeError("symbol must be a string or None")
        for name in ("opened_ns", "closed_ns"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _int_stamp(value, name))


@dataclass(frozen=True, slots=True)
class ReportPositionRow:
    """One position transition shown on the position timeline."""

    transition_id: str
    timestamp_ns: int
    symbol: str
    reason: str
    quantity_before: Decimal
    quantity_after: Decimal
    realized_pnl: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transition_id", str(text(self.transition_id, "transition_id"))
        )
        object.__setattr__(
            self, "timestamp_ns", _int_stamp(self.timestamp_ns, "timestamp_ns")
        )
        object.__setattr__(self, "symbol", str(text(self.symbol, "symbol")))
        object.__setattr__(self, "reason", str(text(self.reason, "reason")))
        for name in ("quantity_before", "quantity_after", "realized_pnl"):
            object.__setattr__(self, name, exact_decimal(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class ReportRiskEvent:
    """One risk decision shown in the risk events table."""

    timestamp_ns: int
    order_id: str
    action: str
    approved: bool
    halted: bool
    reason: str | None = None
    decision_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "timestamp_ns", _int_stamp(self.timestamp_ns, "timestamp_ns")
        )
        object.__setattr__(self, "order_id", str(text(self.order_id, "order_id")))
        object.__setattr__(self, "action", str(text(self.action, "action")))
        if not isinstance(self.approved, bool) or not isinstance(self.halted, bool):
            raise TypeError("approved and halted must be booleans")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("reason must be a string or None")
        if not isinstance(self.decision_id, str):
            raise TypeError("decision_id must be a string")


@dataclass(frozen=True, slots=True)
class ReportInput:
    """Pure value snapshot of one run's artifact data for rendering."""

    header: ReportHeader
    equity: tuple[EquityPoint, ...] = ()
    trades: tuple[ReportTrade, ...] = ()
    positions: tuple[ReportPositionRow, ...] = ()
    risk_events: tuple[ReportRiskEvent, ...] = ()
    fills: tuple[FillRow, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.header, ReportHeader):
            raise TypeError("header must be a ReportHeader")
        prepared = tuple(self.equity)
        for index, point in enumerate(prepared):
            if not isinstance(point, EquityPoint):
                raise TypeError("equity must contain EquityPoint values")
            if point.equity < _ZERO:
                raise ValueError("equity values must be non-negative")
            if index > 0 and point.timestamp_ns < prepared[index - 1].timestamp_ns:
                raise ValueError("equity points must be in ascending order")
        object.__setattr__(self, "equity", prepared)
        for attr, row_type in (
            ("trades", ReportTrade),
            ("positions", ReportPositionRow),
            ("risk_events", ReportRiskEvent),
            ("fills", FillRow),
        ):
            rows = tuple(getattr(self, attr))
            for row in rows:
                if not isinstance(row, row_type):
                    raise TypeError(f"{attr} must contain {row_type.__name__} values")
            object.__setattr__(self, attr, rows)


@dataclass(frozen=True, slots=True)
class _ManifestFacts:
    """Header fields parsed from one recorded manifest artifact."""

    run_id: str
    seed: str
    engine: str
    config_hash: str
    code_fingerprint: str
    metadata: Mapping[str, str]


def _int_stamp(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer nanosecond timestamp")
    return value


def _ns_of(value: object) -> int:
    if not isinstance(value, datetime):
        raise ValueError("row timestamps must be datetimes")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("row timestamps must be timezone-aware")
    delta = value.astimezone(UTC) - _EPOCH
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    ) * 1_000


def _stamp_ns(timestamp_ns: int) -> datetime:
    return _EPOCH + timedelta(microseconds=timestamp_ns // 1_000)


def _fmt_stamp(timestamp_ns: int | None) -> str:
    if timestamp_ns is None:
        return _MISSING
    return _stamp_ns(timestamp_ns).strftime("%Y-%m-%d %H:%M:%S")


def _date_of_ns(timestamp_ns: int) -> str:
    return _stamp_ns(timestamp_ns).date().isoformat()


def _fmt_dec(value: Decimal | None) -> str:
    return _MISSING if value is None else str(value)


def _fmt_float(value: float | None) -> str:
    return _MISSING if value is None else f"{value:.4f}"


def _fmt_duration(duration_ns: int) -> str:
    days, rest = divmod(duration_ns, _NS_PER_DAY)
    hours, rest = divmod(rest, _NS_PER_HOUR)
    minutes, seconds = divmod(rest, _NS_PER_MINUTE)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _fmt_pct(value: Decimal) -> str:
    return f"{float(value) * 100:.2f}%"


def _sample_indices(count: int, limit: int) -> tuple[int, ...]:
    """Return strided indices keeping the first and last entries."""
    if count <= limit:
        return tuple(range(count))
    picked = {(index * count) // (limit - 1) for index in range(limit - 1)}
    picked.add(count - 1)
    return tuple(sorted(picked))


def _sampled[RowT](rows: Sequence[RowT], limit: int) -> tuple[RowT, ...]:
    return tuple(rows[index] for index in _sample_indices(len(rows), limit))


def _ordered_trades(trades: Sequence[ReportTrade]) -> tuple[ReportTrade, ...]:
    return tuple(
        sorted(
            trades,
            key=lambda trade: (
                trade.closed_ns is None,
                trade.closed_ns if trade.closed_ns is not None else 0,
                trade.trade_id,
            ),
        )
    )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _polyline(points: Sequence[tuple[float, float]]) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def _svg(width: int, height: int, label: str, body: str) -> str:
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" aria-label="{_esc(label)}">{body}</svg>'
    )


def _text(
    x: float, y: float, content: str, *, anchor: str = "start", klass: str = "label"
) -> str:
    return (
        f'<text class="{klass}" x="{x:.2f}" y="{y:.2f}" '
        f'text-anchor="{_esc(anchor)}">{_esc(content)}</text>'
    )


def _line(x1: float, y1: float, x2: float, y2: float, klass: str) -> str:
    return (
        f'<line class="{klass}" x1="{x1:.2f}" y1="{y1:.2f}" '
        f'x2="{x2:.2f}" y2="{y2:.2f}" />'
    )


def _circle(x: float, y: float, r: float, klass: str) -> str:
    return f'<circle class="{klass}" cx="{x:.2f}" cy="{y:.2f}" r="{r:.2f}" />'


def _x_map(t0: int, t1: int, left: float, width: float, timestamp_ns: int) -> float:
    if t1 == t0:
        return left + width / 2
    return left + (timestamp_ns - t0) / (t1 - t0) * width


def _equity_chart_svg(
    samples: tuple[EquityPoint, ...], *, width: int, max_points: int
) -> str:
    picked = _sampled(samples, max_points)
    left, right = 92.0, 12.0
    plot_w = width - left - right
    eq_top, eq_h = 18.0, 150.0
    dd_top, dd_h = 210.0, 64.0
    t0, t1 = picked[0].timestamp_ns, picked[-1].timestamp_ns
    values = [point.equity for point in picked]
    low, high = min(values), max(values)
    flat = low == high
    span = _ONE if flat else high - low

    def y_equity(value: Decimal) -> float:
        if flat:
            return eq_top + eq_h / 2
        return eq_top + float((high - value) / span) * eq_h

    marks: list[tuple[float, float]] = [
        (_x_map(t0, t1, left, plot_w, point.timestamp_ns), y_equity(point.equity))
        for point in picked
    ]
    peak = _ZERO
    worst = _ZERO
    dips: list[tuple[float, float]] = []
    for point in picked:
        if point.equity > peak:
            peak = point.equity
        dip = _ZERO if peak == _ZERO else _ONE - point.equity / peak
        worst = max(worst, dip)
        dips.append(
            (
                _x_map(t0, t1, left, plot_w, point.timestamp_ns),
                dd_top + float(dip) * dd_h,
            )
        )
    parts: list[str] = [
        _text(8.0, 14.0, "Equity", klass="title"),
        _text(8.0, 206.0, "Drawdown", klass="title"),
    ]
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = eq_top + fraction * eq_h
        parts.append(_line(left, y, left + plot_w, y, "grid"))
    parts.append(_text(left - 6.0, y_equity(high) + 4.0, str(high), anchor="end"))
    parts.append(_text(left - 6.0, y_equity(low) + 4.0, str(low), anchor="end"))
    if len(picked) == 1:
        parts.append(_circle(marks[0][0], marks[0][1], 3.0, "dot-equity"))
    else:
        parts.append(f'<polyline class="line-equity" points="{_polyline(marks)}" />')
    dd_bottom = dd_top + dd_h
    parts.append(_line(left, dd_bottom, left + plot_w, dd_bottom, "grid"))
    polygon = (
        f'<polygon class="area-dd" points="{_polyline(dips)} '
        f'{dips[-1][0]:.2f},{dd_bottom:.2f} {dips[0][0]:.2f},{dd_bottom:.2f}" />'
    )
    parts.append(polygon)
    parts.append(_text(left - 6.0, dd_top + 4.0, _fmt_pct(worst), anchor="end"))
    if t1 != t0:
        parts.append(_line(left, 196.0, left + plot_w, 196.0, "grid"))
        parts.append(_text(left, 192.0, _date_of_ns(t0)))
        parts.append(
            _text(
                left + plot_w / 2,
                192.0,
                _date_of_ns(t0 + (t1 - t0) // 2),
                anchor="middle",
            )
        )
        parts.append(_text(left + plot_w, 192.0, _date_of_ns(t1), anchor="end"))
    return _svg(width, 300, "Equity curve and drawdown", "".join(parts))


def _trades_chart_svg(
    trades: Sequence[ReportTrade], *, width: int, max_points: int
) -> str | None:
    series: list[tuple[int, Decimal, Decimal]] = []
    cumulative = _ZERO
    for trade in _ordered_trades(trades):
        if trade.closed_ns is None:
            continue
        cumulative += trade.realized_pnl
        series.append((trade.closed_ns, trade.realized_pnl, cumulative))
    if not series:
        return None
    picked = _sampled(series, max_points)
    left, plot_w = 92.0, width - 104.0
    top, plot_h = 18.0, 150.0
    bottom = top + plot_h
    values = [row[2] for row in picked]
    low, high = min(_ZERO, min(values)), max(_ZERO, max(values))
    flat = low == high
    span = _ONE if flat else high - low
    t0, t1 = picked[0][0], picked[-1][0]

    def y_value(value: Decimal) -> float:
        if flat:
            return top + plot_h / 2
        return top + float((high - value) / span) * plot_h

    parts: list[str] = [
        _text(8.0, 14.0, "Cumulative realized PnL", klass="title"),
    ]
    for fraction in (0.0, 0.5, 1.0):
        y = top + fraction * plot_h
        parts.append(_line(left, y, left + plot_w, y, "grid"))
    parts.append(_line(left, bottom, left + plot_w, bottom, "grid"))
    parts.append(_text(left - 6.0, y_value(high) + 4.0, str(high), anchor="end"))
    parts.append(_text(left - 6.0, y_value(low) + 4.0, str(low), anchor="end"))
    coords: list[tuple[float, float]] = []
    for row in picked:
        x = _x_map(t0, t1, left, plot_w, row[0])
        y = y_value(row[2])
        coords.append((x, y))
        parts.append(_circle(x, y, 3.0, _pnl_class(row[1])))
    if t1 != t0:
        parts.append(f'<polyline class="line-equity" points="{_polyline(coords)}" />')
        if low < _ZERO < high:
            parts.append(
                _line(left, y_value(_ZERO), left + plot_w, y_value(_ZERO), "zero")
            )
        parts.append(_text(left, bottom + 14.0, _date_of_ns(t0)))
        parts.append(_text(left + plot_w, bottom + 14.0, _date_of_ns(t1), anchor="end"))
    return _svg(width, 200, "Cumulative realized PnL", "".join(parts))


def _pnl_class(pnl: Decimal) -> str:
    if pnl > _ZERO:
        return "win"
    if pnl < _ZERO:
        return "loss"
    return "flat"


def _positions_chart_svg(
    positions: Sequence[ReportPositionRow], *, width: int, max_points: int
) -> str | None:
    if not positions:
        return None
    ordered = tuple(
        sorted(positions, key=lambda item: (item.timestamp_ns, item.transition_id))
    )
    picked = _sampled(ordered, max_points)
    left = 12.0
    plot_w = width - 24.0
    t0, t1 = picked[0].timestamp_ns, picked[-1].timestamp_ns
    lane = 48.0
    parts: list[str] = [
        _text(8.0, 14.0, "Position transitions", klass="title"),
        _line(left, lane, width - 12.0, lane, "grid"),
    ]
    for row in picked:
        x = _x_map(t0, t1, left, plot_w, row.timestamp_ns)
        parts.append(_circle(x, lane, 3.0, f"tick {row.reason}"))
    parts.append(_text(left, 74.0, _date_of_ns(t0)))
    parts.append(_text(width - 12.0, 74.0, _date_of_ns(t1), anchor="end"))
    return _svg(width, 84, "Position transitions over time", "".join(parts))


def _slippage_svg(values: Sequence[Decimal], *, width: int) -> str:
    counts = [0] * _HISTOGRAM_BINS
    low, high = values[0], values[-1]
    if low == high:
        counts[0] = len(values)
    else:
        step = (high - low) / Decimal(_HISTOGRAM_BINS)
        for value in values:
            offset = int(((value - low) / step).to_integral_value(rounding=ROUND_FLOOR))
            counts[min(max(offset, 0), _HISTOGRAM_BINS - 1)] += 1
    max_count = max(counts)
    left, right = 92.0, 12.0
    plot_w = width - left - right
    top, plot_h = 18.0, 132.0
    bottom = top + plot_h
    parts: list[str] = [
        _text(8.0, 14.0, "Fill slippage distribution", klass="title"),
        _line(left, bottom, left + plot_w, bottom, "grid"),
    ]
    bar_w = plot_w / _HISTOGRAM_BINS
    for index, count in enumerate(counts):
        if not count:
            continue
        height = count / max_count * plot_h
        x = left + index * bar_w
        parts.append(
            f'<rect class="bar" x="{x:.2f}" y="{bottom - height:.2f}" '
            f'width="{max(bar_w - 1.0, 1.0):.2f}" height="{height:.2f}" />'
        )
    if low < _ZERO < high:
        zero_x = left + float((0 - low) / (high - low)) * plot_w
        parts.append(_line(zero_x, top, zero_x, bottom, "zero"))
    parts.append(_text(left - 6.0, top + 10.0, str(max_count), anchor="end"))
    parts.append(_text(left - 6.0, bottom + 4.0, "0", anchor="end"))
    parts.append(_text(left, bottom + 14.0, str(low)))
    parts.append(_text(left + plot_w, bottom + 16.0, str(high), anchor="end"))
    return _svg(width, 176, "Fill slippage distribution", "".join(parts))


def _table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], *, klass: str = ""
) -> str:
    head = "".join(f"<th>{_esc(header)}</th>" for header in headers)
    head_html = f"<thead><tr>{head}</tr></thead>" if headers else ""
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    class_attr = f' class="{klass}"' if klass else ""
    return f"<table{class_attr}>{head_html}<tbody>{body}</tbody></table>"


def _note(message: str) -> str:
    return f'<p class="note">{_esc(message)}</p>'


def _omitted_note(total: int, shown: int, unit: str) -> str:
    if shown >= total:
        return ""
    return _note(
        f"Showing {shown} of {total} {unit} (deterministic stride downsampling)."
    )


def _metric_span(label: str, value: str) -> str:
    return (
        f'<span class="metric"><span class="muted">{_esc(label)}</span> '
        f"<code>{_esc(value)}</code></span>"
    )


def _section(anchor: str, heading: str, body: str) -> str:
    return f'<section id="{_esc(anchor)}"><h2>{_esc(heading)}</h2>{body}</section>'


def _document(run_id: str, sections: str) -> str:
    nav = "".join(
        f'<a href="#{_esc(anchor)}">{_esc(heading)}</a>' for anchor, heading in _NAV
    )
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>FCA Backtest Report \u2014 {_esc(run_id)}</title>",
            f"<style>{_CSS}</style>",
            "</head>",
            "<body>",
            "<main>",
            f"<h1>FCA Backtest Report <code>{_esc(run_id)}</code></h1>",
            f"<nav>{nav}</nav>",
            sections,
            "<footer>Self-contained offline report: inline CSS and SVG only, "
            "no external or network assets.</footer>",
            "</main>",
            "</body>",
            "</html>",
        ]
    )


def _data_range(
    header: ReportHeader, samples: tuple[EquityPoint, ...]
) -> tuple[date | None, date | None]:
    start, end = header.data_start, header.data_end
    if samples:
        if start is None:
            start = _stamp_ns(samples[0].timestamp_ns).date()
        if end is None:
            end = _stamp_ns(samples[-1].timestamp_ns).date()
    return start, end


def _manifest_section(header: ReportHeader, data: ReportInput) -> str:
    start, end = _data_range(header, data.equity)
    rows: list[tuple[str, str]] = [
        ("Run ID", header.run_id),
        ("Engine", header.engine or _MISSING),
        ("Configuration hash", header.config_hash or _MISSING),
        ("Code fingerprint", header.code_fingerprint or _MISSING),
        ("Seed", header.seed or _MISSING),
        ("Data start", start.isoformat() if start else _MISSING),
        ("Data end", end.isoformat() if end else _MISSING),
        ("Schema version", SCHEMA_VERSION),
        ("Equity samples", str(len(data.equity))),
        ("Trades", str(len(data.trades))),
        ("Position transitions", str(len(data.positions))),
        ("Risk decisions", str(len(data.risk_events))),
        ("Fills", str(len(data.fills))),
    ]
    rows.extend(sorted(header.metadata.items()))
    return _section("manifest", "Manifest", _table((), rows, klass="kv"))


def _equity_section(data: ReportInput, *, width: int, max_chart_points: int) -> str:
    if not data.equity:
        return _section("equity", "Equity and drawdown", _note("No equity samples."))
    parts: list[str] = []
    if len(data.equity) >= 2:
        metrics = equity_curve_metrics(data.equity)
        drawdown = metrics.drawdown
        duration = _fmt_duration(drawdown.duration_ns)
        if not drawdown.recovered:
            duration += " (unrecovered)"
        spans = "".join(
            [
                _metric_span("Total return", _fmt_dec(metrics.total_return)),
                _metric_span("CAGR", _fmt_float(metrics.cagr)),
                _metric_span("Max drawdown", _fmt_pct(metrics.max_drawdown)),
                _metric_span("Longest drawdown", duration),
            ]
        )
        parts.append(f'<p class="metrics">{spans}</p>')
    parts.append(
        _equity_chart_svg(data.equity, width=width, max_points=max_chart_points)
    )
    return _section("equity", "Equity and drawdown", "".join(parts))


def _trades_section(
    data: ReportInput, *, width: int, max_chart_points: int, max_table_rows: int
) -> str:
    if not data.trades:
        return _section("trades", "Trades", _note("No realized trades."))
    parts: list[str] = []
    chart = _trades_chart_svg(data.trades, width=width, max_points=max_chart_points)
    if chart is not None:
        parts.append(chart)
    cumulative = _ZERO
    rows: list[tuple[str, ...]] = []
    for trade in _ordered_trades(data.trades):
        cumulative += trade.realized_pnl
        rows.append(
            (
                _fmt_stamp(trade.closed_ns),
                _fmt_stamp(trade.opened_ns),
                trade.trade_id,
                trade.strategy_id or _MISSING,
                trade.symbol or _MISSING,
                str(trade.realized_pnl),
                str(cumulative),
            )
        )
    shown = _sampled(rows, max_table_rows)
    parts.append(
        _table(
            ("Closed", "Opened", "Trade", "Strategy", "Symbol", "PnL", "Cumulative"),
            shown,
        )
    )
    parts.append(_omitted_note(len(rows), len(shown), "trades"))
    return _section("trades", "Trades", "".join(parts))


def _positions_section(
    data: ReportInput, *, width: int, max_chart_points: int, max_table_rows: int
) -> str:
    if not data.positions:
        return _section("positions", "Positions", _note("No position transitions."))
    parts: list[str] = []
    chart = _positions_chart_svg(
        data.positions, width=width, max_points=max_chart_points
    )
    if chart is not None:
        parts.append(chart)
    ordered = tuple(
        sorted(data.positions, key=lambda item: (item.timestamp_ns, item.transition_id))
    )
    rows = [
        (
            _fmt_stamp(row.timestamp_ns),
            row.transition_id,
            row.symbol,
            row.reason,
            str(row.quantity_before),
            str(row.quantity_after),
            str(row.realized_pnl),
        )
        for row in ordered
    ]
    shown = _sampled(rows, max_table_rows)
    parts.append(
        _table(
            (
                "Time",
                "Transition",
                "Symbol",
                "Reason",
                "Before",
                "After",
                "Realized PnL",
            ),
            shown,
        )
    )
    parts.append(_omitted_note(len(rows), len(shown), "transitions"))
    return _section("positions", "Positions", "".join(parts))


def _risk_section(data: ReportInput, *, max_table_rows: int) -> str:
    if not data.risk_events:
        return _section("risk", "Risk events", _note("No risk decisions."))
    ordered = tuple(
        sorted(
            data.risk_events,
            key=lambda item: (item.timestamp_ns, item.decision_id, item.order_id),
        )
    )
    rows = [
        (
            _fmt_stamp(event.timestamp_ns),
            event.decision_id or _MISSING,
            event.order_id,
            event.action,
            "yes" if event.approved else "no",
            "yes" if event.halted else "no",
            event.reason if event.reason is not None else _MISSING,
        )
        for event in ordered
    ]
    shown = _sampled(rows, max_table_rows)
    body = _table(
        ("Time", "Decision", "Order", "Action", "Approved", "Halted", "Reason"),
        shown,
    )
    return _section(
        "risk", "Risk events", body + _omitted_note(len(rows), len(shown), "decisions")
    )


def _slippage_section(data: ReportInput, *, width: int) -> str:
    if not data.fills:
        return _section("slippage", "Slippage", _note("No fills."))
    values = sorted(fill.slippage for fill in data.fills)
    total = sum(values, _ZERO)
    fees = sum(sorted(fill.fee for fill in data.fills), _ZERO)
    mean = total / Decimal(len(values))
    spans = "".join(
        [
            _metric_span("Fills", str(len(values))),
            _metric_span("Slippage total", str(total)),
            _metric_span("Slippage mean", str(mean)),
            _metric_span("Slippage min", str(values[0])),
            _metric_span("Slippage max", str(values[-1])),
            _metric_span("Fees total", str(fees)),
        ]
    )
    body = f'<p class="metrics">{spans}</p>{_slippage_svg(tuple(values), width=width)}'
    return _section("slippage", "Slippage", body)


def _check_render_params(
    max_chart_points: int, max_table_rows: int, width: int
) -> None:
    if isinstance(max_chart_points, bool) or not isinstance(max_chart_points, int):
        raise TypeError("max_chart_points must be an integer")
    if max_chart_points < 2:
        raise ValueError("max_chart_points must be at least 2")
    if isinstance(max_table_rows, bool) or not isinstance(max_table_rows, int):
        raise TypeError("max_table_rows must be an integer")
    if max_table_rows < 1:
        raise ValueError("max_table_rows must be a positive integer")
    if isinstance(width, bool) or not isinstance(width, int):
        raise TypeError("width must be an integer")
    if not _MIN_WIDTH <= width <= _MAX_WIDTH:
        raise ValueError(f"width must be between {_MIN_WIDTH} and {_MAX_WIDTH}")


def render_report(
    data: ReportInput,
    *,
    max_chart_points: int = _DEFAULT_CHART_POINTS,
    max_table_rows: int = _DEFAULT_TABLE_ROWS,
    width: int = _DEFAULT_WIDTH,
) -> str:
    """Render one run's artifact data as a self-contained HTML document.

    The document is a pure function of ``data`` and the downsampling
    keyword arguments: no wall clock, randomness, I/O, or network access
    participates, and repeated calls with the same arguments return
    byte-identical strings.
    """
    _check_render_params(max_chart_points, max_table_rows, width)
    sections = "\n".join(
        [
            _manifest_section(data.header, data),
            _equity_section(data, width=width, max_chart_points=max_chart_points),
            _trades_section(
                data,
                width=width,
                max_chart_points=max_chart_points,
                max_table_rows=max_table_rows,
            ),
            _positions_section(
                data,
                width=width,
                max_chart_points=max_chart_points,
                max_table_rows=max_table_rows,
            ),
            _risk_section(data, max_table_rows=max_table_rows),
            _slippage_section(data, width=width),
        ]
    )
    return _document(data.header.run_id, sections)


def render_report_bytes(
    data: ReportInput,
    *,
    max_chart_points: int = _DEFAULT_CHART_POINTS,
    max_table_rows: int = _DEFAULT_TABLE_ROWS,
    width: int = _DEFAULT_WIDTH,
) -> bytes:
    """Return :func:`render_report` as deterministic UTF-8 bytes."""
    return render_report(
        data,
        max_chart_points=max_chart_points,
        max_table_rows=max_table_rows,
        width=width,
    ).encode("utf-8")


def report_input_from_directory(
    directory: str | Path,
    *,
    header: ReportHeader | None = None,
    fills: Sequence[FillRow] = (),
) -> ReportInput:
    """Load :class:`ReportInput` from one recorded run directory.

    The manifest, equity, position-transition, and risk-decision artifacts
    are read with the recorder's schema-validated reader; a missing
    optional artifact renders as an empty section.  Trades are derived
    from position transitions that realize nonzero PnL, and fills carrying
    reference-price slippage must be supplied by callers that know the
    reference prices, via ``fills``.
    """
    root = Path(directory)
    manifest_rows = read_rows(artifact_path(root, Artifact.MANIFEST), Artifact.MANIFEST)
    facts = _manifest_facts(manifest_rows)
    resolved = (
        header
        if header is not None
        else ReportHeader(
            run_id=facts.run_id,
            engine=facts.engine,
            config_hash=facts.config_hash,
            code_fingerprint=facts.code_fingerprint,
            seed=facts.seed,
            metadata=facts.metadata,
        )
    )
    equity = tuple(
        EquityPoint(
            timestamp_ns=_ns_of(row["timestamp"]),
            equity=exact_decimal(value, "equity"),
        )
        for row in _optional_rows(root, Artifact.EQUITY)
        if (value := row.get("equity")) is not None
    )
    positions = tuple(
        sorted(
            (
                ReportPositionRow(
                    transition_id=str(row["transition_id"]),
                    timestamp_ns=_ns_of(row["timestamp"]),
                    symbol=str(row["symbol"]),
                    reason=str(row["reason"]),
                    quantity_before=exact_decimal(
                        row["quantity_before"], "quantity_before"
                    ),
                    quantity_after=exact_decimal(
                        row["quantity_after"], "quantity_after"
                    ),
                    realized_pnl=exact_decimal(row["realized_pnl"], "realized_pnl"),
                )
                for row in _optional_rows(root, Artifact.POSITION_TRANSITIONS)
            ),
            key=lambda item: (item.timestamp_ns, item.transition_id),
        )
    )
    risk_events = tuple(
        sorted(
            (
                ReportRiskEvent(
                    timestamp_ns=_ns_of(row["timestamp"]),
                    order_id=str(row["order_id"]),
                    action=str(row["action"]),
                    approved=bool(row["approved"]),
                    halted=bool(row["halted"]),
                    reason=None if row.get("reason") is None else str(row["reason"]),
                    decision_id=str(row["decision_id"]),
                )
                for row in _optional_rows(root, Artifact.RISK_DECISIONS)
            ),
            key=lambda item: (item.timestamp_ns, item.decision_id, item.order_id),
        )
    )
    trades = tuple(
        ReportTrade(
            trade_id=row.transition_id,
            realized_pnl=row.realized_pnl,
            symbol=row.symbol,
            closed_ns=row.timestamp_ns,
        )
        for row in positions
        if row.realized_pnl != _ZERO
    )
    return ReportInput(
        header=resolved,
        equity=equity,
        trades=trades,
        positions=positions,
        risk_events=risk_events,
        fills=tuple(fills),
    )


def _optional_rows(
    directory: Path, artifact: Artifact
) -> tuple[dict[str, object], ...]:
    try:
        return read_rows(artifact_path(directory, artifact), artifact)
    except FileNotFoundError:
        return ()


def _manifest_facts(rows: tuple[dict[str, object], ...]) -> _ManifestFacts:
    run_id = ""
    seed = ""
    engine = ""
    config_hash = ""
    fingerprint = ""
    metadata: dict[str, str] = {}
    for row in rows:
        key = str(row.get("key", ""))
        value = str(row.get("value", ""))
        if key == "run_id":
            run_id = value
        elif key == "seed":
            seed = value
        elif key == "engine":
            engine = value
        elif key in {"configuration_hash", "config_hash"}:
            config_hash = value
        elif key == "code_fingerprint":
            fingerprint = value
        elif key.startswith("metadata:"):
            metadata[key.removeprefix("metadata:")] = value
    return _ManifestFacts(
        run_id=run_id or "unknown",
        seed=seed,
        engine=engine,
        config_hash=config_hash,
        code_fingerprint=fingerprint,
        metadata=MappingProxyType(metadata),
    )


__all__ = [
    "ReportHeader",
    "ReportInput",
    "ReportPositionRow",
    "ReportRiskEvent",
    "ReportTrade",
    "report_input_from_directory",
    "render_report",
    "render_report_bytes",
]
