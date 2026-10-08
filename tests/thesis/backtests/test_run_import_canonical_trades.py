"""Canonical ``metrics.json`` import behavior.

The importer reads the harness-published ``equity`` and ``trades`` blocks
verbatim and projects the canonical trade rows.  Nothing is recomputed: profit
factor is never derived from gross profit, a trade log is never rebuilt from
fills, and a value the harness did not publish is ``None`` with a reason.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest

from thesis.agent.harness_tools import HarnessToolProvider, HarnessToolResult
from thesis.backtests.run_import import BacktestRunError, import_backtest_run
from thesis.integrations.mcp import TOOL_GET_JOB_RESULT, TOOL_GET_JOB_STATUS

_CATALOG: dict[str, object] = {
    "state": "succeeded",
    "strategy_id": "strategy-1",
    "data_start": "2024-01-02T14:30:00Z",
    "data_end": "2024-01-02T15:00:00Z",
}

_BACKTEST: dict[str, object] = {
    "status": "completed",
    "manifest": {"start": "2024-01-02T14:30:00Z", "end": "2024-01-02T15:00:00Z"},
    "portfolio": {"initial_cash": "1000"},
    "fills": [
        {
            "symbol": "SPY",
            "side": "buy",
            "quantity": 1,
            "price": 10,
            "fee": 0,
            "timestamp": 1704205800000,
            "sequence": 1,
        },
        {
            "symbol": "SPY",
            "side": "sell",
            "quantity": 1,
            "price": 12,
            "fee": 0,
            "timestamp": 1704205860000,
            "sequence": 2,
        },
    ],
}

_EQUITY: dict[str, object] = {
    "total_return": "0.10",
    "max_drawdown": "0.02",
    "cagr": "0.10",
}

# Two closed slices whose aggregates match ``_canonical_trades``: net profit 6,
# net loss 2, so profit factor 3 and hit rate 0.5.  Timestamps are exact
# nanosecond integers far beyond float precision.
_ROWS: list[dict[str, object]] = [
    {
        "trade_id": "trade-0001",
        "symbol": "SPY",
        "side": "long",
        "quantity": "1.000000000000000000",
        "entry_sequence": 1,
        "exit_sequence": 2,
        "entry_timestamp_ns": 1704205800000000123,
        "exit_timestamp_ns": 1704205860000000456,
        "entry_price": "10.000000000000000000",
        "exit_price": "16.000000000000000000",
        "gross_pnl": "6.000000000000000000",
        "entry_fee": "0.000000000000000000",
        "exit_fee": "0.000000000000000000",
        "fees": "0.000000000000000000",
        "net_pnl": "6.000000000000000000",
    },
    {
        "trade_id": "trade-0002",
        "symbol": "SPY",
        "side": "short",
        "quantity": "1.000000000000000000",
        "entry_sequence": 3,
        "exit_sequence": 4,
        "entry_timestamp_ns": 1704205920000000000,
        "exit_timestamp_ns": 1704205980000000000,
        "entry_price": "12.000000000000000000",
        "exit_price": "14.000000000000000000",
        "gross_pnl": "-2.000000000000000000",
        "entry_fee": "0.000000000000000000",
        "exit_fee": "0.000000000000000000",
        "fees": "0.000000000000000000",
        "net_pnl": "-2.000000000000000000",
    },
]


class _Tools(HarnessToolProvider):
    def __init__(self, catalog: Mapping[str, object] = _CATALOG) -> None:
        self._catalog_document = catalog

    def call_tool(self, name, arguments=None):
        arguments = arguments or {}
        if name == TOOL_GET_JOB_STATUS:
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={"job_id": arguments["job_id"], "state": "succeeded"},
            )
        if name == TOOL_GET_JOB_RESULT:
            return HarnessToolResult(
                tool=name, ok=True, structured=self._catalog_document
            )
        raise AssertionError(f"unexpected tool {name!r}")


class _Artifacts:
    def __init__(
        self,
        metrics: Mapping[str, object],
        backtest: Mapping[str, object] = _BACKTEST,
    ) -> None:
        self._metrics = metrics
        self._backtest = backtest

    def read(self, job_id: str, name: str) -> bytes | None:
        if name == "metrics.json":
            return json.dumps(self._metrics).encode("utf-8")
        if name == "backtest.json":
            return json.dumps(self._backtest).encode("utf-8")
        return None


def _import(
    metrics: Mapping[str, object],
    backtest: Mapping[str, object] = _BACKTEST,
):
    return import_backtest_run(
        tools=cast(HarnessToolProvider, _Tools()),
        workspace_id="workspace-1",
        job_id="run-1",
        artifact_reader=_Artifacts(metrics, backtest),
        clock=lambda: datetime(2024, 1, 2, tzinfo=UTC),
    )


def _stats(record) -> Mapping[str, object]:
    return cast(Mapping[str, object], record.manifest["stats"])


def _canonical_trades(**overrides: object) -> dict[str, object]:
    trades: dict[str, object] = {
        "trade_count": 2,
        "win_count": 1,
        "loss_count": 1,
        "hit_rate": "0.5",
        "net_pnl": "4.0",
        "gross_profit": "6.0",
        "gross_loss": "2.0",
        "profit_factor": "3.0",
        "average_net_win": "6.0",
        "average_net_loss": "-2.0",
        "largest_net_win": "6.0",
        "largest_net_loss": "-2.0",
        "rows": _ROWS,
    }
    trades.update(overrides)
    return trades


def test_reads_every_canonical_trade_field_in_snake_and_camel_case() -> None:
    record = _import({"equity": _EQUITY, "trades": _canonical_trades()})
    stats = _stats(record)
    for snake, camel, expected in [
        ("trade_count", "tradeCount", 2.0),
        ("win_count", "winCount", 1.0),
        ("loss_count", "lossCount", 1.0),
        ("hit_rate", "hitRate", 0.5),
        ("win_rate", None, 0.5),
        ("net_pnl", "totalPnl", 4.0),
        ("gross_profit", "grossProfit", 6.0),
        ("gross_loss", "grossLoss", 2.0),
        ("profit_factor", "profitFactor", 3.0),
        ("average_net_win", "averageWin", 6.0),
        ("average_net_loss", "averageLoss", -2.0),
        ("largest_net_win", "largestWin", 6.0),
        ("largest_net_loss", "largestLoss", -2.0),
    ]:
        assert stats[snake] == expected, snake
        if camel is not None:
            assert stats[camel] == expected, camel
    assert stats["trades_unavailable_reason"] is None
    assert record.trades == 2
    assert record.win_rate == 0.5
    assert record.total_return == 0.10
    assert len(record.trade_log) == 2


def test_trade_log_projects_each_canonical_row_with_exact_timestamps() -> None:
    record = _import({"equity": _EQUITY, "trades": _canonical_trades()})
    first, second = record.trade_log
    assert first == {
        "id": "trade-0001",
        "symbol": "SPY",
        "side": "buy",
        "qty": 1.0,
        "entryTime": 1704205800000,
        "entryPrice": 10.0,
        "exitTime": 1704205860000,
        "exitPrice": 16.0,
        "grossPnl": 6.0,
        "fees": 0.0,
        "pnl": 6.0,
    }
    assert second["side"] == "sell"
    assert second["pnl"] == -2.0


def test_missing_trades_block_emits_undefined_and_no_reconstructed_trades() -> None:
    record = _import({"equity": _EQUITY})
    stats = _stats(record)
    for key in (
        "trade_count",
        "win_count",
        "loss_count",
        "hit_rate",
        "win_rate",
        "net_pnl",
        "gross_profit",
        "gross_loss",
        "profit_factor",
        "average_net_win",
        "average_net_loss",
        "largest_net_win",
        "largest_net_loss",
    ):
        assert stats[key] is None, key
    for key in (
        "tradeCount",
        "winCount",
        "lossCount",
        "hitRate",
        "totalPnl",
        "grossProfit",
        "grossLoss",
        "profitFactor",
        "averageWin",
        "averageLoss",
        "largestWin",
        "largestLoss",
    ):
        assert stats[key] is None, key
    # The fills contain a completed round trip; it is not rebuilt into trades.
    assert record.trade_log == ()
    assert record.trades is None
    assert record.win_rate is None
    assert stats["trades_unavailable_reason"] == "legacy_artifact"
    assert record.total_return == 0.10


def test_trades_block_without_rows_is_a_legacy_artifact_not_a_rebuild() -> None:
    trades = _canonical_trades()
    trades.pop("rows")
    record = _import({"equity": _EQUITY, "trades": trades})
    assert record.trade_log == ()
    assert record.trades == 2
    assert _stats(record)["trades_unavailable_reason"] == "legacy_artifact"


def test_malformed_canonical_row_fails_the_import_instead_of_defaulting() -> None:
    broken = [dict(row) for row in _ROWS]
    broken[1].pop("net_pnl")
    with pytest.raises(BacktestRunError, match="breaks the canonical contract"):
        _import({"equity": _EQUITY, "trades": _canonical_trades(rows=broken)})


def test_missing_metrics_leave_every_statistic_undefined() -> None:
    record = _import({})
    stats = _stats(record)
    assert record.total_return is None
    assert record.sharpe is None
    assert record.max_drawdown is None
    assert record.win_rate is None
    assert record.trades is None
    assert record.final_capital is None
    assert stats["trades_unavailable_reason"] == "metrics_unavailable"
    assert stats["equity_unavailable_reason"] == "artifact_unavailable"
    assert record.initial_capital == 1000.0


def test_sharpe_is_read_from_the_harness_with_its_undefined_reason() -> None:
    published = _import({"equity": {**_EQUITY, "sharpe": "1.25"}})
    assert published.sharpe == 1.25
    assert _stats(published)["sharpe_reason"] is None
    undefined = _import(
        {
            "equity": {
                **_EQUITY,
                "sharpe": None,
                "sharpe_reason": "fewer_than_three_samples",
            }
        }
    )
    assert undefined.sharpe is None
    assert _stats(undefined)["sharpe_reason"] == "fewer_than_three_samples"


def test_capital_change_is_derived_from_published_end_capital() -> None:
    backtest = {**_BACKTEST, "portfolio": {"initial_cash": "1000", "equity": "1100"}}
    record = _import({"equity": _EQUITY, "trades": _canonical_trades()}, backtest)
    assert record.initial_capital == 1000.0
    assert record.final_capital == 1100.0
    detail = record.detail()
    assert detail["capital_change"] == 100.0
    assert "net_pnl" not in detail


def test_capital_change_stays_undefined_without_published_end_capital() -> None:
    record = _import({"equity": _EQUITY, "trades": _canonical_trades()})
    assert record.final_capital is None
    assert record.detail()["capital_change"] is None


@pytest.mark.parametrize("profit_factor", [None, "absent"])
def test_no_loss_never_falls_back_to_gross_profit(profit_factor: object) -> None:
    trades = _canonical_trades(
        trade_count=1,
        win_count=1,
        loss_count=0,
        hit_rate="1.0",
        net_pnl="6.0",
        gross_profit="6.0",
        gross_loss="0E-18",
        average_net_loss=None,
        largest_net_loss=None,
        rows=[],
    )
    if profit_factor == "absent":
        trades.pop("profit_factor")
    else:
        trades["profit_factor"] = None
    record = _import({"equity": _EQUITY, "trades": trades})
    stats = _stats(record)
    assert stats["gross_profit"] == 6.0
    assert stats["gross_loss"] == 0.0
    assert stats["profit_factor"] is None
    assert stats["profitFactor"] is None
