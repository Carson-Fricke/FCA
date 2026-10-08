"""Focused job-keyed import and materialization tests for backtest reports."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from thesis.agent.harness_tools import HarnessToolProvider, HarnessToolResult
from thesis.backtests.coordinator import BacktestRunCoordinator
from thesis.backtests.run_import import (
    BacktestRunError,
    BacktestRunPending,
    import_backtest_run,
)
from thesis.http.backtests import (
    BacktestDataService,
    BacktestRecord,
    InMemoryBacktestStore,
)

JOB_ID = "job-0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeTools(HarnessToolProvider):
    """Job-keyed MCP responses plus artifact bytes without a transport."""

    def __init__(
        self,
        responses: Mapping[str, HarnessToolResult],
        documents: Mapping[str, object] | None = None,
    ) -> None:
        self._responses = dict(responses)
        self._documents = dict(documents or {})
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> HarnessToolResult:
        resolved = dict(arguments or {})
        self.calls.append((name, resolved))
        if name == "read_artifact":
            document = self._documents.get(str(resolved.get("name")))
            if document is None:
                return HarnessToolResult(tool=name, ok=True, structured={})
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={"content": json.dumps(document), "encoding": "utf-8"},
            )
        if name == "list_artifacts":
            return HarnessToolResult(
                tool=name, ok=True, structured={"job_id": JOB_ID, "artifacts": []}
            )
        return self._responses[name]


class FakeReader:
    """Injected artifact reader over in-memory job documents."""

    def __init__(self, documents: Mapping[str, object]) -> None:
        self._documents = dict(documents)

    def read(self, job_id: str, name: str) -> bytes | None:
        document = self._documents.get(name)
        if document is None:
            return None
        if isinstance(document, bytes):
            return document
        return json.dumps(document).encode()


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _document(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


ARTIFACTS: dict[str, object] = {
    "metrics.json": _document("closed_trades.metrics.json"),
    "backtest.json": _document("closed_trades.backtest.json"),
}


def _tools(state: str = "succeeded") -> FakeTools:
    return FakeTools(
        {
            "get_job_status": HarnessToolResult(
                tool="get_job_status",
                ok=True,
                structured={"job_id": JOB_ID, "state": state},
            ),
            "get_job_result": HarnessToolResult(
                tool="get_job_result",
                ok=True,
                structured={
                    "job_id": JOB_ID,
                    "kind": "backtest",
                    "state": "succeeded",
                    "strategy_id": "momentum",
                    "data_start": "2024-01-01T00:00:00Z",
                    "data_end": "2024-01-02T00:00:00Z",
                    "artifact_count": 2,
                    "total_bytes": 2048,
                },
            ),
        },
        documents=ARTIFACTS,
    )


def _record(name: str) -> BacktestRecord:
    return BacktestRecord(
        job_id=JOB_ID,
        workspace_id="workspace-one",
        strategy_id="momentum",
        name=name,
        period_start="2024-01-01T00:00:00Z",
        period_end="2024-01-02T00:00:00Z",
        created_at="2026-01-01T00:00:00Z",
        initial_capital=1000.0,
        final_capital=1100.0,
        total_return=0.1,
        sharpe_per_sample=1.0,
        max_drawdown=-0.05,
        win_rate=0.5,
        trades=2,
    )


def test_import_keys_the_record_and_manifest_by_job_id() -> None:
    record = import_backtest_run(
        tools=_tools(),
        workspace_id="workspace-one",
        job_id=JOB_ID,
        artifact_reader=FakeReader(ARTIFACTS),
        clock=lambda: NOW,
    )

    assert record.job_id == JOB_ID
    assert record.status == "succeeded"
    assert record.sharpe_per_sample == -0.41
    assert record.manifest["job_id"] == JOB_ID
    assert "run_id" not in record.manifest
    detail = record.detail()
    assert detail["job_id"] == JOB_ID
    assert "id" not in detail


def test_pending_and_failed_jobs_do_not_import() -> None:
    for state, error in (
        ("running", BacktestRunPending),
        ("queued", BacktestRunPending),
        ("failed", BacktestRunError),
        ("cancelled", BacktestRunError),
    ):
        with pytest.raises(error):
            import_backtest_run(
                tools=_tools(state),
                workspace_id="workspace-one",
                job_id=JOB_ID,
                artifact_reader=FakeReader(ARTIFACTS),
            )


def test_materialization_creates_one_projection_per_workspace_job() -> None:
    store = InMemoryBacktestStore()
    service = BacktestDataService(store=store, clock=lambda: NOW)
    coordinator = BacktestRunCoordinator(service, _tools(), clock=lambda: NOW)

    first = coordinator.materialize("workspace-one", JOB_ID)
    assert first["state"] == "succeeded"
    assert "status" not in first
    assert first["job_id"] == JOB_ID
    assert "backtest_id" not in first
    backtest = first["backtest"]
    assert isinstance(backtest, dict) and backtest["job_id"] == JOB_ID

    repeated = coordinator.materialize("workspace-one", JOB_ID)
    assert repeated["backtest"] == first["backtest"]
    assert len(store.list("workspace-one")) == 1


def test_tool_result_enrichment_uses_job_id_only() -> None:
    store = InMemoryBacktestStore()
    service = BacktestDataService(store=store, clock=lambda: NOW)
    coordinator = BacktestRunCoordinator(service, _tools(), clock=lambda: NOW)

    running = coordinator.handle_tool_result(
        "workspace-one",
        HarnessToolResult(
            tool="run_backtest",
            ok=True,
            structured={"job_id": JOB_ID, "state": "queued"},
        ),
        chat_id="chat-1",
        execution_id="execution-1",
    )
    assert running == {
        "job_id": JOB_ID,
        "state": "queued",
        "chat_id": "chat-1",
        "execution_id": "execution-1",
    }
    assert store.list("workspace-one") == ()

    terminal = coordinator.handle_tool_result(
        "workspace-one",
        HarnessToolResult(
            tool="get_job_result",
            ok=True,
            structured={"job_id": JOB_ID, "state": "succeeded"},
        ),
        chat_id="chat-1",
    )
    assert terminal is not None
    assert terminal["job_id"] == JOB_ID
    assert "backtest_id" not in terminal
    records = store.list("workspace-one")
    assert len(records) == 1 and records[0].job_id == JOB_ID


def test_store_add_is_atomic_add_if_absent_under_concurrency() -> None:
    store = InMemoryBacktestStore()
    records = [_record(f"attempt-{index}") for index in range(8)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        stored = list(pool.map(store.add, records))

    assert len(store.list("workspace-one")) == 1
    assert all(item is stored[0] for item in stored)
