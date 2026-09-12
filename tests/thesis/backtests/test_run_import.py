"""Importer and coordinator coverage for harness run import.

A fake in-process MCP handler serves the real captured artifact shapes
(``get_run`` catalog row, ``backtest.json``, ``metrics.json``, and an optional
``equity.parquet``), so the importer maps a finished harness run into a
workspace :class:`~thesis.http.backtests.BacktestRecord` without any harness
process.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from thesis.agent.harness_tools import (
    HARNESS_TOOL_NAMES,
    HarnessToolProvider,
    HarnessToolResult,
    open_harness_tool_provider,
)
from thesis.backtests import (
    BacktestRunCoordinator,
    BacktestRunError,
    BacktestRunPending,
    import_backtest_run,
)
from thesis.http.backtests import BacktestDataService, InMemoryBacktestStore
from thesis.integrations.mcp import HarnessMcpTopology

WORKSPACE = "workspace-a"
RUN_ID = "momentum-spy-2024-01"


def _reply(message: Mapping[str, object], result: Mapping[str, object]):
    return {"jsonrpc": "2.0", "id": message.get("id"), "result": dict(result)}


class _FakeHarnessHandler:
    """In-process MCP handler serving scripted run and artifact responses."""

    def __init__(
        self,
        *,
        catalog: Mapping[str, object] | None = None,
        artifacts: Mapping[str, Mapping[str, str]] | None = None,
        statuses: Sequence[str] = (),
        catalog_missing: bool = False,
    ) -> None:
        self.catalog = dict(catalog or {})
        self.artifacts = {
            name: dict(value) for name, value in (artifacts or {}).items()
        }
        self.statuses = list(statuses)
        self.catalog_missing = catalog_missing
        self.calls: list[dict[str, object]] = []

    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        if "id" not in message:
            return None
        method = message.get("method")
        if method == "initialize":
            return _reply(message, {"protocolVersion": "2024-11-05"})
        if method == "tools/list":
            return _reply(
                message,
                {
                    "tools": [
                        {"name": name, "description": name, "inputSchema": {}}
                        for name in HARNESS_TOOL_NAMES
                    ]
                },
            )
        if method == "tools/call":
            params = message.get("params")
            if not isinstance(params, Mapping):
                raise TypeError("params must be a mapping")
            name = str(params.get("name"))
            arguments = params.get("arguments")
            arguments = dict(arguments) if isinstance(arguments, Mapping) else {}
            self.calls.append({"name": name, "arguments": arguments})
            return _reply(message, self._result(name, arguments))
        return _reply(message, {})

    def _result(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        if name == "get_run":
            if self.catalog_missing:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": "run does not exist"}],
                    "structuredContent": {},
                }
            return {
                "isError": False,
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": dict(self.catalog),
            }
        if name == "get_backtest_status":
            state = self.statuses.pop(0) if self.statuses else "complete"
            return {
                "isError": False,
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {
                    "job_id": arguments.get("job_id"),
                    "state": state,
                },
            }
        if name == "read_artifact":
            artifact_name = str(arguments.get("name"))
            artifact = self.artifacts.get(artifact_name)
            if artifact is None:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": "artifact does not exist"}],
                    "structuredContent": {},
                }
            return {
                "isError": False,
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {
                    "run_id": arguments.get("run_id"),
                    "name": artifact_name,
                    **artifact,
                },
            }
        return {
            "isError": False,
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {},
        }


def _provider(handler: _FakeHarnessHandler) -> HarnessToolProvider:
    return open_harness_tool_provider(
        HarnessMcpTopology(thesis_location="local", harness_location="local"),
        in_process_handler=handler,
    )


def _catalog(**overrides: object) -> dict[str, object]:
    return {
        "run_id": RUN_ID,
        "status": "complete",
        "strategy_id": "spy-momentum",
        "data_start": "2024-01-02T14:30:00Z",
        "data_end": "2024-01-19T21:00:00Z",
        "run_digest": "a" * 64,
        "configuration_digest": "b" * 64,
        "artifact_count": 5,
        "total_bytes": 25_277,
        "metrics": {},
        **overrides,
    }


def _backtest_document() -> dict[str, object]:
    return {
        "manifest": {
            "run_id": RUN_ID,
            "start": "2024-01-02T14:30:00+00:00",
            "end": "2024-01-03T21:00:00+00:00",
            "initial_cash": "10000.00",
        },
        "fills": [
            {
                "fill_id": "f1",
                "timestamp": "2024-01-02T15:00:00+00:00",
                "order_id": "o1",
                "symbol": "SPY",
                "side": "buy",
                "quantity": "10",
                "price": "100",
                "fee": "1",
                "sequence": 1,
                "event_id": "e1",
            },
            {
                "fill_id": "f2",
                "timestamp": "2024-01-02T16:00:00+00:00",
                "order_id": "o2",
                "symbol": "SPY",
                "side": "sell",
                "quantity": "10",
                "price": "110",
                "fee": "1",
                "sequence": 2,
                "event_id": "e2",
            },
            {
                "fill_id": "f3",
                "timestamp": "2024-01-02T17:00:00+00:00",
                "order_id": "o3",
                "symbol": "SPY",
                "side": "sell",
                "quantity": "5",
                "price": "120",
                "fee": "1",
                "sequence": 3,
                "event_id": "e3",
            },
            {
                "fill_id": "f4",
                "timestamp": "2024-01-02T18:00:00+00:00",
                "order_id": "o4",
                "symbol": "SPY",
                "side": "buy",
                "quantity": "5",
                "price": "115",
                "fee": "1",
                "sequence": 4,
                "event_id": "e4",
            },
        ],
        "portfolio": {
            "initial_cash": "10000.00",
            "cash": "10121.0",
            "equity": "10121.0",
            "total_pnl": "121.0",
            "realized_pnl": "121.0",
            "positions": [],
            "books": [],
        },
        "counts": {"fills": 4},
        "status": "completed",
    }


def _metrics_document() -> dict[str, object]:
    return {
        "counts": {"fills": 4},
        "equity": {
            "cagr": "0.2",
            "max_drawdown": "-0.03",
            "total_return": "0.05",
            "samples": 4,
        },
        "execution": {
            "fees_total": "4.0",
            "fill_count": 4,
            "slippage_total": "0.0",
        },
        "run_id": RUN_ID,
        "status": "completed",
    }


def _artifacts(*, include_equity_parquet: bool = False) -> dict[str, dict[str, str]]:
    artifacts = {
        "backtest.json": {
            "encoding": "utf-8",
            "content": json.dumps(_backtest_document()),
        },
        "metrics.json": {
            "encoding": "utf-8",
            "content": json.dumps(_metrics_document()),
        },
        "config.yaml": {
            "encoding": "utf-8",
            "content": f"shell: backtest\nrun_id: {RUN_ID}\n",
        },
    }
    if include_equity_parquet:
        table = pa.table(
            {
                "timestamp": pa.array(
                    [
                        datetime(2024, 1, 2, 15, 0, tzinfo=UTC),
                        datetime(2024, 1, 2, 18, 0, tzinfo=UTC),
                    ],
                    type=pa.timestamp("ns", tz="UTC"),
                ),
                "equity": pa.array(["10000.0", "10121.0"]),
            }
        )
        buffer = io.BytesIO()
        pq.write_table(table, buffer)
        artifacts["artifacts/equity.parquet"] = {
            "encoding": "hex",
            "content": buffer.getvalue().hex(),
        }
    return artifacts


def test_import_maps_run_to_record_with_links_and_stats() -> None:
    handler = _FakeHarnessHandler(catalog=_catalog(), artifacts=_artifacts())
    record = import_backtest_run(
        tools=_provider(handler),
        workspace_id=WORKSPACE,
        run_id=RUN_ID,
        chat_id="chat-1",
        execution_id="exec-1",
        clock=lambda: datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        id_factory=lambda prefix: f"{prefix}-001",
    )

    assert record.id == "bt-001"
    assert record.workspace_id == WORKSPACE
    assert record.strategy_id == "spy-momentum"
    assert record.run_id == RUN_ID
    assert record.chat_id == "chat-1"
    assert record.execution_id == "exec-1"
    assert record.status == "completed"
    assert record.initial_capital == 10_000.0
    assert record.final_capital == 10_121.0
    assert record.period_start == "2024-01-02T14:30:00Z"
    assert record.period_end == "2024-01-19T21:00:00Z"

    assert len(record.equity) == 5
    assert record.equity[-1]["v"] == 10_121.0
    assert all(isinstance(point["t"], int) for point in record.equity)

    assert len(record.trade_log) == 2
    first, second = record.trade_log
    assert first["side"] == "buy"
    assert first["qty"] == 10.0
    assert first["entryPrice"] == 100.0
    assert first["exitPrice"] == 110.0
    assert first["pnl"] == 100.0
    assert second["side"] == "sell"
    assert second["pnl"] == 25.0

    assert record.stats["total_return"] == 0.05
    assert record.stats["max_drawdown"] == -0.03
    assert record.stats["cagr"] == 0.2
    assert record.stats["win_rate"] == 1.0
    assert record.stats["trade_count"] == 2.0
    assert record.stats["total_pnl"] == 121.0
    assert record.stats["hitRate"] == 1.0
    assert record.stats["totalReturn"] == 0.05
    assert record.stats["maxDrawdown"] == -0.03
    assert record.stats["tradeCount"] == 2.0
    assert "sharpe" in record.stats

    row = record.summary_row()
    assert row["run_id"] == RUN_ID
    assert row["chat_id"] == "chat-1"
    assert row["execution_id"] == "exec-1"
    assert row["trades"] == 2

    detail = record.detail()
    manifest = cast(Mapping[str, object], detail["manifest"])
    assert detail["run_id"] == RUN_ID
    assert record.config_path == "config.yaml"
    assert "shell: backtest" in str(manifest["configuration"])
    assert manifest["run_digest"] == "a" * 64
    assert manifest["configuration_digest"] == "b" * 64
    assert len(cast(Sequence[object], detail["trades"])) == 2
    stats = cast(Mapping[str, object], detail["stats"])
    assert stats["tradeCount"] == 2.0


def test_import_prefers_the_equity_parquet_when_available() -> None:
    handler = _FakeHarnessHandler(
        catalog=_catalog(), artifacts=_artifacts(include_equity_parquet=True)
    )
    record = import_backtest_run(
        tools=_provider(handler), workspace_id=WORKSPACE, run_id=RUN_ID
    )

    assert len(record.equity) == 2
    timestamps = [point["t"] for point in record.equity]
    assert timestamps == sorted(timestamps)
    assert record.final_capital == 10_121.0


def test_import_raises_pending_while_running() -> None:
    handler = _FakeHarnessHandler(
        catalog=_catalog(status="running"), artifacts=_artifacts()
    )
    with pytest.raises(BacktestRunPending, match="running"):
        import_backtest_run(
            tools=_provider(handler), workspace_id=WORKSPACE, run_id=RUN_ID
        )


def test_import_raises_error_when_failed() -> None:
    handler = _FakeHarnessHandler(
        catalog=_catalog(status="failed"), artifacts=_artifacts()
    )
    with pytest.raises(BacktestRunError):
        import_backtest_run(
            tools=_provider(handler), workspace_id=WORKSPACE, run_id=RUN_ID
        )


def test_import_raises_error_when_run_missing() -> None:
    handler = _FakeHarnessHandler(catalog_missing=True)
    with pytest.raises(BacktestRunError):
        import_backtest_run(
            tools=_provider(handler), workspace_id=WORKSPACE, run_id=RUN_ID
        )


class _FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now = self._now + timedelta(seconds=1)
        return self._now


def _run_result(state: str) -> HarnessToolResult:
    return HarnessToolResult(
        tool="run_backtest",
        ok=True,
        structured={"boundary": "local", "run_id": RUN_ID, "state": state},
    )


def test_coordinator_reports_running_without_blocking() -> None:
    handler = _FakeHarnessHandler(
        catalog=_catalog(), artifacts=_artifacts(), statuses=["running"]
    )
    service = BacktestDataService(InMemoryBacktestStore())
    coordinator = BacktestRunCoordinator(
        service,
        _provider(handler),
        clock=_FakeClock(),
        id_factory=lambda prefix: f"{prefix}-001",
    )

    enrichment = coordinator.handle_tool_result(
        WORKSPACE,
        _run_result("running"),
        chat_id="chat-1",
        execution_id="exec-1",
    )

    assert enrichment == {
        "run_id": RUN_ID,
        "state": "running",
        "chat_id": "chat-1",
        "execution_id": "exec-1",
    }
    assert service.store.list(WORKSPACE) == ()


def test_coordinator_imports_a_completed_get_run() -> None:
    handler = _FakeHarnessHandler(catalog=_catalog(), artifacts=_artifacts())
    service = BacktestDataService(InMemoryBacktestStore())
    coordinator = BacktestRunCoordinator(
        service, _provider(handler), id_factory=lambda prefix: f"{prefix}-001"
    )
    tool_result = HarnessToolResult(
        tool="get_run",
        ok=True,
        structured={
            "run_id": RUN_ID,
            "status": "complete",
            "strategy_id": "spy-momentum",
        },
    )

    enrichment = coordinator.handle_tool_result(
        WORKSPACE, tool_result, chat_id="chat-1", execution_id="exec-1"
    )

    assert enrichment is not None
    assert enrichment["backtest_id"] == "bt-001"
    assert enrichment["run_id"] == RUN_ID
    records = service.store.list(WORKSPACE)
    assert len(records) == 1
    assert records[0].chat_id == "chat-1"
    assert records[0].execution_id == "exec-1"


def test_materialize_returns_complete_card_and_is_idempotent() -> None:
    handler = _FakeHarnessHandler(catalog=_catalog(), artifacts=_artifacts())
    service = BacktestDataService(InMemoryBacktestStore())
    coordinator = BacktestRunCoordinator(
        service, _provider(handler), id_factory=lambda prefix: f"{prefix}-001"
    )

    card = coordinator.materialize(
        WORKSPACE, RUN_ID, chat_id="chat-1", execution_id="exec-1"
    )
    assert card["status"] == "complete"
    assert card["run_id"] == RUN_ID
    assert card["backtest_id"] == "bt-001"
    backtest = cast(Mapping[str, object], card["backtest"])
    assert backtest["run_id"] == RUN_ID
    assert len(service.store.list(WORKSPACE)) == 1

    again = coordinator.materialize(WORKSPACE, RUN_ID)
    assert again["backtest_id"] == "bt-001"
    assert len(service.store.list(WORKSPACE)) == 1


def test_materialize_returns_pending_while_running() -> None:
    handler = _FakeHarnessHandler(
        catalog=_catalog(), artifacts=_artifacts(), statuses=["running"]
    )
    service = BacktestDataService(InMemoryBacktestStore())
    coordinator = BacktestRunCoordinator(service, _provider(handler))

    card = coordinator.materialize(WORKSPACE, RUN_ID)

    assert card == {"status": "pending", "run_id": RUN_ID, "state": "running"}
    assert service.store.list(WORKSPACE) == ()


def test_materialize_reports_failure() -> None:
    handler = _FakeHarnessHandler(
        catalog=_catalog(status="failed"), artifacts=_artifacts(), statuses=["failed"]
    )
    service = BacktestDataService(InMemoryBacktestStore())
    coordinator = BacktestRunCoordinator(service, _provider(handler))

    card = coordinator.materialize(WORKSPACE, RUN_ID)

    assert card["status"] == "failed"
    assert service.store.list(WORKSPACE) == ()
