"""Canonical ``metrics.json`` import behavior over hand-written harness fixtures.

A case is ``fixtures/<case>.metrics.json`` in the harness publication schema
(``closed_trades.v1`` trades and the ``equity`` block), paired with
``fixtures/<case>.backtest.json`` when the case needs a fill ledger.  The
importer reads the published blocks verbatim: nothing is recomputed, profit
factor is never derived from gross profit, a trade log is never rebuilt from
fills, and a value the harness did not publish is ``None`` with a reason.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest

from thesis.agent.harness_tools import HarnessToolProvider, HarnessToolResult
from thesis.backtests.run_import import BacktestRunError, import_backtest_run
from thesis.http.backtests import BacktestRecord
from thesis.integrations.mcp import TOOL_GET_JOB_RESULT, TOOL_GET_JOB_STATUS

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
JOB_ID = "job-0123456789abcdef0123456789abcdef"
WORKSPACE_ID = "workspace-1"
NOW = datetime(2024, 1, 2, tzinfo=UTC)

_CATALOG: dict[str, object] = {
    "state": "succeeded",
    "strategy_id": "strategy-1",
    "data_start": "2024-01-02T14:30:00Z",
    "data_end": "2024-01-02T14:35:00Z",
}

# The workspace projection of closed_trades.metrics.json ``trades.rows``, in row
# order.  Nanosecond timestamps are truncated to the workspace milliseconds.
_EXPECTED_TRADES: list[dict[str, object]] = [
    {
        "id": "trade-0001",
        "symbol": "SPY",
        "side": "buy",
        "qty": 4.0,
        "entryTime": 1704205800000,
        "entryPrice": 10.0,
        "exitTime": 1704205860000,
        "exitPrice": 12.0,
        "grossPnl": 8.0,
        "fees": 0.8,
        "pnl": 7.2,
    },
    {
        "id": "trade-0002",
        "symbol": "SPY",
        "side": "buy",
        "qty": 6.0,
        "entryTime": 1704205800000,
        "entryPrice": 10.0,
        "exitTime": 1704205920000,
        "exitPrice": 9.6,
        "grossPnl": -2.4,
        "fees": 1.2,
        "pnl": -3.6,
    },
    {
        "id": "trade-0003",
        "symbol": "SPY",
        "side": "sell",
        "qty": 5.0,
        "entryTime": 1704205980000,
        "entryPrice": 20.0,
        "exitTime": 1704206040000,
        "exitPrice": 21.0,
        "grossPnl": -5.0,
        "fees": 1.0,
        "pnl": -6.0,
    },
]


class _Tools(HarnessToolProvider):
    """Status and result of one succeeded harness job, with no harness client."""

    def __init__(self) -> None:
        pass

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> HarnessToolResult:
        if name == TOOL_GET_JOB_STATUS:
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={"job_id": JOB_ID, "state": "succeeded"},
            )
        if name == TOOL_GET_JOB_RESULT:
            return HarnessToolResult(tool=name, ok=True, structured=_CATALOG)
        raise AssertionError(f"unexpected tool {name!r}")


class _Artifacts:
    """Fixture bytes served as the job's artifacts; an absent document is ``None``."""

    def __init__(self, metrics: bytes | None, backtest: bytes | None) -> None:
        self._documents: dict[str, bytes | None] = {
            "metrics.json": metrics,
            "backtest.json": backtest,
        }

    def read(self, job_id: str, name: str) -> bytes | None:
        return self._documents.get(name)


def _fixture(name: str) -> bytes | None:
    path = FIXTURES / name
    return path.read_bytes() if path.is_file() else None


def _run(metrics: bytes | None, backtest: bytes | None = None) -> BacktestRecord:
    return import_backtest_run(
        tools=_Tools(),
        workspace_id=WORKSPACE_ID,
        job_id=JOB_ID,
        artifact_reader=_Artifacts(metrics, backtest),
        clock=lambda: NOW,
    )


def _case(name: str, *, backtest: str | None = None) -> BacktestRecord:
    """Import ``<name>.metrics.json`` with its fill ledger when one exists.

    ``backtest`` names another case whose ``backtest.json`` is read instead, so a
    published metrics document can be paired with a ledger that would produce
    different trades.
    """
    metrics = _fixture(f"{name}.metrics.json")
    assert metrics is not None, f"missing fixture {name}.metrics.json"
    return _run(metrics, _fixture(f"{backtest or name}.backtest.json"))


def _stats(record: BacktestRecord) -> Mapping[str, object]:
    return cast(Mapping[str, object], record.manifest["stats"])


def test_reads_every_published_statistic_in_both_key_styles() -> None:
    record = _case("closed_trades")
    stats = _stats(record)
    for snake, camel, expected in [
        ("trade_count", "tradeCount", 3.0),
        ("win_count", "winCount", 1.0),
        ("loss_count", "lossCount", 2.0),
        ("hit_rate", "hitRate", pytest.approx(1 / 3)),
        ("win_rate", None, pytest.approx(1 / 3)),
        ("gross_profit", "grossProfit", 8.0),
        ("gross_loss", "grossLoss", 7.4),
        ("net_profit", "netProfit", 7.2),
        ("net_loss", "netLoss", 9.6),
        ("profit_factor", "profitFactor", 0.75),
        ("average_net_win", "averageWin", 7.2),
        ("average_net_loss", "averageLoss", -4.8),
        ("largest_net_win", "largestWin", 7.2),
        ("largest_net_loss", "largestLoss", -6.0),
        ("total_return", "totalReturn", -0.0024),
        ("max_drawdown", "maxDrawdown", 0.02),
        ("sharpe_per_sample", "sharpePerSample", -0.41),
    ]:
        assert stats[snake] == expected, snake
        if camel is not None:
            assert stats[camel] == expected, camel
    for reason in (
        "total_return_reason",
        "max_drawdown_reason",
        "sharpe_per_sample_reason",
        "profit_factor_reason",
        "hit_rate_reason",
        "trades_unavailable_reason",
    ):
        assert stats[reason] is None, reason
    assert record.trades == 3
    assert record.win_rate == pytest.approx(1 / 3)
    assert record.total_return == -0.0024
    assert record.max_drawdown == 0.02
    assert record.sharpe_per_sample == -0.41
    assert len(record.trade_log) == 3


def test_net_pnl_is_the_headline_with_explicit_gross_and_fees() -> None:
    stats = _stats(_case("closed_trades"))
    assert stats["net_pnl"] == -2.4
    assert stats["totalPnl"] == stats["net_pnl"]
    assert stats["gross_pnl"] == 0.6
    assert stats["grossPnl"] == stats["gross_pnl"]
    assert stats["fees"] == 3.0
    # Gross less fees reconciles to the net headline; the headline is not the gross.
    assert stats["gross_pnl"] - stats["fees"] == pytest.approx(stats["net_pnl"])


def test_trade_log_projects_published_rows_with_ids_and_exact_times() -> None:
    record = _case("closed_trades")
    assert [trade["id"] for trade in record.trade_log] == [
        "trade-0001",
        "trade-0002",
        "trade-0003",
    ]
    assert list(record.trade_log) == _EXPECTED_TRADES


def test_trade_log_ignores_fills_that_would_publish_other_trades() -> None:
    # The ledger holds one 10-unit round trip (net -12); reconstructing it from
    # fills would yield one trade, not the three published rows.
    record = _case("closed_trades", backtest="fifo_mismatch")
    assert list(record.trade_log) == _EXPECTED_TRADES
    assert record.trades == 3
    assert _stats(record)["net_pnl"] == -2.4


def test_legacy_artifact_without_trades_is_not_rebuilt_from_fills() -> None:
    # The ledger holds a closed round trip; it must not be rebuilt into a trade.
    record = _case("legacy_no_trades")
    stats = _stats(record)
    assert record.trade_log == ()
    assert record.trades is None
    assert record.win_rate is None
    assert stats["trades_unavailable_reason"] == "legacy_artifact"
    for key in (
        "trade_count",
        "tradeCount",
        "win_count",
        "winCount",
        "loss_count",
        "lossCount",
        "hit_rate",
        "hitRate",
        "win_rate",
        "net_pnl",
        "totalPnl",
        "gross_pnl",
        "grossPnl",
        "fees",
        "gross_profit",
        "grossProfit",
        "gross_loss",
        "grossLoss",
        "net_profit",
        "netProfit",
        "net_loss",
        "netLoss",
        "profit_factor",
        "profitFactor",
        "average_net_win",
        "averageWin",
        "average_net_loss",
        "averageLoss",
        "largest_net_win",
        "largestWin",
        "largest_net_loss",
        "largestLoss",
    ):
        assert stats[key] is None, key
    # The equity block is published independently of the trades block.
    assert record.total_return == 0.002
    assert stats["sharpe_per_sample_reason"] == "fewer_than_three_returns"


def test_undefined_profit_factor_is_null_with_its_reason() -> None:
    stats = _stats(_case("no_net_losses"))
    assert stats["gross_profit"] == 4.0
    assert stats["net_profit"] == 3.6
    assert stats["profit_factor"] is None
    assert stats["profitFactor"] is None
    assert stats["profit_factor_reason"] == "no_net_losses"
    assert stats["hit_rate"] == 1.0
    assert stats["average_net_loss"] is None


def test_null_drawdown_is_null_with_its_reason() -> None:
    record = _case("negative_equity")
    stats = _stats(record)
    assert record.max_drawdown is None
    assert stats["max_drawdown"] is None
    assert stats["maxDrawdown"] is None
    assert stats["max_drawdown_reason"] == "negative_equity"
    assert record.summary_row()["max_drawdown"] is None
    assert record.total_return is None
    assert stats["total_return_reason"] == "negative_equity"
    assert stats["sharpe_per_sample"] is None
    assert stats["sharpe_per_sample_reason"] == "negative_equity"
    # A published zero is a value: a 0% hit rate and a zero profit factor are not missing.
    assert record.win_rate == 0.0
    assert stats["profit_factor"] == 0.0


def test_malformed_canonical_row_fails_the_import_instead_of_defaulting() -> None:
    metrics = _fixture("malformed_trade_id.metrics.json")
    assert metrics is not None
    with pytest.raises(BacktestRunError, match="breaks the canonical contract"):
        _run(metrics)


def test_missing_metrics_artifact_leaves_every_statistic_undefined() -> None:
    record = _run(None)
    stats = _stats(record)
    assert record.total_return is None
    assert record.sharpe_per_sample is None
    assert record.max_drawdown is None
    assert record.win_rate is None
    assert record.trades is None
    assert record.trade_log == ()
    assert stats["trades_unavailable_reason"] == "metrics_unavailable"
    assert stats["equity_unavailable_reason"] == "artifact_unavailable"


def test_capital_change_is_derived_from_the_published_end_capital() -> None:
    record = _case("closed_trades")
    assert record.initial_capital == 1000.0
    assert record.final_capital == 997.6
    assert record.detail()["capital_change"] == -2.4


def test_capital_change_stays_undefined_without_a_published_end_capital() -> None:
    record = _case("legacy_no_trades")
    assert record.initial_capital == 1000.0
    assert record.final_capital is None
    assert record.detail()["capital_change"] is None
