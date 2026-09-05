"""Focused tests for the offline static HTML report renderer."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from harness.results.metrics import EquityPoint, FillRow
from harness.results.recorder import (
    RECORDER_SCHEMAS,
    Artifact,
    artifact_path,
)
from harness.results.report import (
    ReportHeader,
    ReportInput,
    ReportPositionRow,
    ReportRiskEvent,
    ReportTrade,
    render_report,
    render_report_bytes,
    report_input_from_directory,
)

D: Final = Decimal
NS_DAY: Final = 86_400_000_000_000
MISSING: Final = "\u2014"


def points(*values: str) -> tuple[EquityPoint, ...]:
    """Build a daily equity curve from decimal string marks."""
    return tuple(
        EquityPoint(timestamp_ns=day * NS_DAY, equity=D(value))
        for day, value in enumerate(values)
    )


def make_header(**overrides: object) -> ReportHeader:
    """Build one manifest header with deterministic facts."""
    fields: dict[str, object] = {
        "run_id": "run-2024-01-02",
        "engine": "python-pure",
        "config_hash": "cfg-123",
        "code_fingerprint": "fp-456",
        "seed": "7",
        "data_start": date(2024, 1, 2),
        "data_end": date(2024, 1, 4),
        "metadata": {"experiment": "T100"},
    }
    fields.update(overrides)
    return ReportHeader(**fields)


def make_trades() -> tuple[ReportTrade, ...]:
    """Build three realized trades with hand-computed cumulative PnL."""
    return (
        ReportTrade(
            "trade-1", D("50"), strategy_id="alpha", symbol="SPY", closed_ns=NS_DAY
        ),
        ReportTrade(
            "trade-2",
            D("-30"),
            strategy_id="alpha",
            symbol="SPY",
            closed_ns=2 * NS_DAY,
        ),
        ReportTrade(
            "trade-3", D("20"), strategy_id="beta", symbol="AAPL", closed_ns=3 * NS_DAY
        ),
    )


def make_positions() -> tuple[ReportPositionRow, ...]:
    """Build three position transitions with hand-computed quantities."""
    return (
        ReportPositionRow("pos-1", NS_DAY, "SPY", "fill", D("0"), D("10"), D("0")),
        ReportPositionRow(
            "pos-2", 2 * NS_DAY, "SPY", "close", D("10"), D("0"), D("-30")
        ),
        ReportPositionRow("pos-3", 3 * NS_DAY, "AAPL", "fill", D("5"), D("15"), D("5")),
    )


def make_risk_events() -> tuple[ReportRiskEvent, ...]:
    """Build one approved and one rejected risk decision."""
    return (
        ReportRiskEvent(
            NS_DAY,
            "order-1",
            "approve",
            True,
            False,
            reason="within limits",
            decision_id="decision-1",
        ),
        ReportRiskEvent(
            2 * NS_DAY,
            "order-2",
            "reject",
            False,
            True,
            reason="limit breach",
            decision_id="decision-2",
        ),
    )


def make_fills() -> tuple[FillRow, ...]:
    """Build three fills with hand-computed signed slippage."""
    return tuple(
        FillRow(
            fill_id=f"fill-{index}",
            order_id=f"order-{index}",
            strategy_id="alpha",
            quantity=D("1"),
            price=D("100"),
            fee=D("0.5"),
            slippage=D(slippage),
        )
        for index, slippage in enumerate(("1.5", "-0.5", "2.0"))
    )


def make_input(**overrides: object) -> ReportInput:
    """Build one small artifact set covering every report section."""
    fields: dict[str, object] = {
        "header": make_header(),
        "equity": points("100", "80", "120", "90"),
        "trades": make_trades(),
        "positions": make_positions(),
        "risk_events": make_risk_events(),
        "fills": make_fills(),
    }
    fields.update(overrides)
    return ReportInput(**fields)


def test_manifest_header_renders_every_spec_fact() -> None:
    html = render_report(make_input())

    assert ">run-2024-01-02</code>" in html
    assert "<td>python-pure</td>" in html
    assert "<td>cfg-123</td>" in html
    assert "<td>fp-456</td>" in html
    assert "<td>7</td>" in html
    assert "<td>2024-01-02</td>" in html
    assert "<td>2024-01-04</td>" in html
    assert "<td>experiment</td><td>T100</td>" in html
    assert "<td>1</td>" in html


def test_report_sections_render_in_spec_order() -> None:
    html = render_report(make_input())

    anchors = [
        html.index(f'id="{anchor}"')
        for anchor in ("manifest", "equity", "trades", "positions", "risk", "slippage")
    ]
    assert anchors == sorted(anchors)
    assert html.count("<svg") == 4


def test_equity_and_drawdown_chart_is_hand_computed() -> None:
    html = render_report(make_input())

    polyline = html.split('class="line-equity" points="', 1)[1].split('"', 1)[0]
    assert len(polyline.split(" ")) == 4
    assert 'polygon class="area-dd"' in html
    assert '<span class="muted">Total return</span> <code>-0.1</code>' in html
    assert '<span class="muted">CAGR</span> <code>0.0000</code>' in html
    assert '<span class="muted">Max drawdown</span> <code>25.00%</code>' in html
    assert ">25.00%</text>" in html
    assert '<span class="muted">Longest drawdown</span> <code>2d 0h</code>' in html


def test_trade_timeline_rows_and_cumulative_pnl() -> None:
    html = render_report(make_input())

    assert (
        "<td>1970-01-02 00:00:00</td><td>\u2014</td><td>trade-1</td>"
        "<td>alpha</td><td>SPY</td><td>50</td><td>50</td>"
    ) in html
    assert "<td>-30</td><td>20</td>" in html
    assert "<td>20</td><td>40</td>" in html
    assert 'class="win"' in html
    assert 'class="loss"' in html


def test_position_timeline_rows_and_ticks() -> None:
    html = render_report(make_input())

    assert (
        "<td>1970-01-02 00:00:00</td><td>pos-1</td><td>SPY</td><td>fill</td>"
        "<td>0</td><td>10</td><td>0</td>"
    ) in html
    assert 'class="tick fill"' in html
    assert 'class="tick close"' in html


def test_risk_events_table_rows() -> None:
    html = render_report(make_input())

    assert (
        "<td>1970-01-02 00:00:00</td><td>decision-1</td><td>order-1</td>"
        "<td>approve</td><td>yes</td><td>no</td><td>within limits</td>"
    ) in html
    assert "<td>reject</td><td>no</td><td>yes</td><td>limit breach</td>" in html


def test_slippage_distribution_is_hand_computed() -> None:
    html = render_report(make_input())

    assert '<span class="muted">Slippage total</span> <code>3.0</code>' in html
    assert '<span class="muted">Slippage mean</span> <code>1.0</code>' in html
    assert '<span class="muted">Slippage min</span> <code>-0.5</code>' in html
    assert '<span class="muted">Slippage max</span> <code>2.0</code>' in html
    assert '<span class="muted">Fees total</span> <code>1.5</code>' in html
    assert html.count('<rect class="bar"') == 3
    assert 'class="zero"' in html


def test_render_is_deterministic_and_byte_identical() -> None:
    first = render_report(make_input())
    second = render_report(make_input())

    assert first == second
    assert render_report_bytes(make_input()) == first.encode("utf-8")


def test_render_is_invariant_to_input_ordering() -> None:
    base = render_report(
        make_input(
            header=make_header(metadata={"b": "2", "a": "1"}),
            fills=tuple(reversed(make_fills())),
        )
    )
    other = render_report(
        make_input(
            header=make_header(metadata={"a": "1", "b": "2"}),
            fills=make_fills(),
        )
    )

    assert base == other


def test_report_contains_no_external_or_network_assets() -> None:
    html = render_report(make_input())
    lowered = html.lower()

    assert "http" not in lowered
    assert "<script" not in lowered
    assert "src=" not in lowered
    assert "@import" not in lowered
    assert "url(" not in lowered
    assert "<iframe" not in lowered
    assert lowered.count("<style>") == 1


def test_empty_input_renders_placeholders_without_crashing() -> None:
    html = render_report(ReportInput(header=make_header()))

    assert "No equity samples." in html
    assert "No realized trades." in html
    assert "No position transitions." in html
    assert "No risk decisions." in html
    assert "No fills." in html
    assert "<td>0</td>" in html


def test_data_range_derives_from_equity_when_header_omits_it() -> None:
    html = render_report(make_input(header=make_header(data_start=None, data_end=None)))

    assert "<td>1970-01-01</td>" in html
    assert "<td>1970-01-04</td>" in html
    assert "<td>2024-01-02</td>" not in html


def test_downsampling_caps_chart_points_and_table_rows() -> None:
    curve = tuple(
        EquityPoint(timestamp_ns=day * NS_DAY, equity=D("1000")) for day in range(500)
    )
    trades = tuple(
        ReportTrade(f"trade-{index:04d}", D("1"), closed_ns=index * NS_DAY)
        for index in range(500)
    )
    data = make_input(equity=curve, trades=trades)

    html = render_report(data, max_chart_points=50, max_table_rows=10)

    polyline = html.split('class="line-equity" points="', 1)[1].split('"', 1)[0]
    assert len(polyline.split(" ")) == 50
    assert html.count("trade-") == 10
    assert "Showing 10 of 500 trades" in html


def test_five_year_report_stays_under_five_megabytes() -> None:
    curve = tuple(
        EquityPoint(timestamp_ns=day * NS_DAY, equity=D("100000") + D(day))
        for day in range(5 * 365 + 2)
    )
    trades = tuple(
        ReportTrade(
            f"trade-{index:05d}",
            D("25") if index % 3 else D("-12"),
            strategy_id="alpha",
            symbol="SPY",
            closed_ns=3 * index * NS_DAY,
        )
        for index in range(1300)
    )
    positions = tuple(
        ReportPositionRow(
            f"pos-{index:05d}",
            index * NS_DAY,
            "SPY",
            "fill",
            D(index % 5),
            D(index % 5 + 1),
            D("0"),
        )
        for index in range(2600)
    )
    risk_events = tuple(
        ReportRiskEvent(
            index * NS_DAY,
            f"order-{index:05d}",
            "approve",
            True,
            False,
            reason="within limits",
            decision_id=f"decision-{index:05d}",
        )
        for index in range(1300)
    )
    fills = tuple(
        FillRow(
            fill_id=f"fill-{index:05d}",
            order_id=f"order-{index:05d}",
            strategy_id="alpha",
            quantity=D("1"),
            price=D("100"),
            fee=D("0.02"),
            slippage=D(index % 7) - D(3),
        )
        for index in range(5200)
    )
    data = make_input(
        equity=curve,
        trades=trades,
        positions=positions,
        risk_events=risk_events,
        fills=fills,
    )

    payload = render_report_bytes(data)

    assert len(payload) < 5_000_000
    assert b"Showing" in payload


def _stamp(day: int) -> datetime:
    return datetime(1970, 1, 1 + day, tzinfo=UTC)


def _write_artifact(
    tmp_path, artifact: Artifact, rows: list[dict[str, object]]
) -> None:
    table = pa.Table.from_pylist(
        [dict(row) for row in rows], schema=RECORDER_SCHEMAS[artifact]
    )
    pq.write_table(table, artifact_path(tmp_path, artifact))


def write_run_directory(tmp_path) -> None:
    """Write a minimal recorded run directory with hand-built Parquet rows."""
    _write_artifact(
        tmp_path,
        Artifact.MANIFEST,
        [
            {"key": "run_id", "value": "run-42"},
            {"key": "seed", "value": "7"},
            {"key": "engine", "value": "python-pure"},
            {"key": "configuration_hash", "value": "cfg-abc"},
            {"key": "code_fingerprint", "value": "fp-9"},
            {"key": "metadata:experiment", "value": "T100"},
        ],
    )
    _write_artifact(
        tmp_path,
        Artifact.EQUITY,
        [
            {
                "snapshot_id": "eq-1",
                "sequence": 0,
                "timestamp": _stamp(1),
                "account_id": "acct",
                "book_id": None,
                "source": "portfolio",
                "cash": "900",
                "equity": "1000",
                "market_value": "100",
                "realized_pnl": "0",
                "unrealized_pnl": None,
                "total_pnl": None,
                "position_count": 1,
                "fill_count": 0,
            },
            {
                "snapshot_id": "eq-2",
                "sequence": 1,
                "timestamp": _stamp(2),
                "account_id": "acct",
                "book_id": None,
                "source": "portfolio",
                "cash": "850",
                "equity": None,
                "market_value": None,
                "realized_pnl": "-25",
                "unrealized_pnl": None,
                "total_pnl": None,
                "position_count": 0,
                "fill_count": 1,
            },
            {
                "snapshot_id": "eq-3",
                "sequence": 2,
                "timestamp": _stamp(3),
                "account_id": "acct",
                "book_id": None,
                "source": "portfolio",
                "cash": "960",
                "equity": "1100",
                "market_value": "140",
                "realized_pnl": "60",
                "unrealized_pnl": None,
                "total_pnl": None,
                "position_count": 2,
                "fill_count": 2,
            },
        ],
    )
    _write_artifact(
        tmp_path,
        Artifact.POSITION_TRANSITIONS,
        [
            {
                "transition_id": "pos-1",
                "sequence": 0,
                "timestamp": _stamp(2),
                "account_id": "acct",
                "book_id": None,
                "symbol": "SPY",
                "reason": "close",
                "fill_id": "fill-1",
                "quantity_before": "10",
                "quantity_after": "0",
                "cost_basis": "0",
                "cash_flow": "1005",
                "realized_pnl": "-25",
                "mark_price": None,
                "market_value": None,
                "unrealized_pnl": None,
            }
        ],
    )
    _write_artifact(
        tmp_path,
        Artifact.RISK_DECISIONS,
        [
            {
                "decision_id": "decision-1",
                "sequence": 0,
                "timestamp": _stamp(2),
                "strategy_id": "alpha",
                "action_id": None,
                "order_id": "order-9",
                "action": "reject",
                "approved": False,
                "halted": True,
                "reason": "limit breach",
                "reasons": '["limit breach"]',
                "audit_count": 1,
            }
        ],
    )


def test_report_input_from_directory_parses_recorded_run(tmp_path) -> None:
    write_run_directory(tmp_path)

    data = report_input_from_directory(tmp_path)

    assert data.header.run_id == "run-42"
    assert data.header.engine == "python-pure"
    assert data.header.config_hash == "cfg-abc"
    assert data.header.code_fingerprint == "fp-9"
    assert data.header.seed == "7"
    assert dict(data.header.metadata) == {"experiment": "T100"}
    assert [point.equity for point in data.equity] == [D("1000"), D("1100")]
    assert data.equity[1].timestamp_ns == 3 * NS_DAY
    assert len(data.positions) == 1
    assert data.positions[0].reason == "close"
    assert data.trades[0].realized_pnl == D("-25")
    assert data.trades[0].closed_ns == 2 * NS_DAY
    assert len(data.risk_events) == 1
    assert data.risk_events[0].halted is True
    html = render_report(data)
    assert 'id="equity"' in html
    assert "<code>run-42</code>" in html


def test_loader_accepts_caller_supplied_slippage_fills(tmp_path) -> None:
    write_run_directory(tmp_path)

    data = report_input_from_directory(tmp_path, fills=make_fills())
    html = render_report(data)

    assert '<span class="muted">Slippage total</span> <code>3.0</code>' in html


def test_loader_treats_missing_optional_artifacts_as_empty(tmp_path) -> None:
    _write_artifact(
        tmp_path,
        Artifact.MANIFEST,
        [
            {"key": "run_id", "value": "run-manifest-only"},
            {"key": "seed", "value": "1"},
        ],
    )

    data = report_input_from_directory(tmp_path)
    html = render_report(data)

    assert data.header.run_id == "run-manifest-only"
    assert data.equity == ()
    assert data.trades == ()
    assert data.positions == ()
    assert data.risk_events == ()
    assert "No equity samples." in html
    assert "No realized trades." in html
    assert "No position transitions." in html
    assert "No risk decisions." in html
    assert "No fills." in html


def test_report_input_validation() -> None:
    with pytest.raises(ValueError, match="ascending"):
        make_input(
            equity=(EquityPoint(2 * NS_DAY, D("100")), EquityPoint(NS_DAY, D("90")))
        )
    with pytest.raises(ValueError, match="non-negative"):
        make_input(equity=points("100", "-1"))
    with pytest.raises(TypeError):
        make_input(trades=(1,))  # type: ignore[list-item]
    with pytest.raises(TypeError):
        cast(Any, ReportTrade)("trade", 1.5)
    with pytest.raises(TypeError):
        ReportHeader(run_id="run", data_start=datetime(2024, 1, 2, tzinfo=UTC))
    with pytest.raises(ValueError):
        ReportHeader(
            run_id="run", data_start=date(2024, 1, 3), data_end=date(2024, 1, 2)
        )


def test_render_parameter_validation() -> None:
    data = make_input()

    with pytest.raises(ValueError, match="at least 2"):
        render_report(data, max_chart_points=1)
    with pytest.raises(ValueError, match="positive"):
        render_report(data, max_table_rows=0)
    with pytest.raises(ValueError, match="width"):
        render_report(data, width=100)
