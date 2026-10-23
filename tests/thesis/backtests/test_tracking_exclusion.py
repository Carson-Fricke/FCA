"""Harness report surfaces exclude non-harness tracking jobs."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from datetime import UTC, datetime

from thesis.agent.harness_tools import (
    HarnessToolFailure,
    HarnessToolProvider,
    HarnessToolResult,
)
from thesis.application.job_classification import HARNESS_RESEARCH_MODEL_TOOL
from thesis.backtests.coordinator import BacktestRunCoordinator
from thesis.backtests.records import InMemoryBacktestStore
from thesis.http.backtests import BacktestDataService
from thesis.integrations.mcp import (
    TOOL_GET_JOB_RESULT,
    TOOL_GET_JOB_STATUS,
    TOOL_LIST_ARTIFACTS,
    TOOL_READ_ARTIFACT,
)
from thesis_runtime.contracts.records import ToolJob, ToolJobState

JOB_ID = "job-0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 1, 1, tzinfo=UTC)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _document(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


ARTIFACTS: dict[str, object] = {
    "metrics.json": _document("closed_trades.metrics.json"),
    "backtest.json": _document("closed_trades.backtest.json"),
}


class FakeTools(HarnessToolProvider):
    """Job-keyed MCP responses with recorded calls and no transport."""

    def __init__(
        self,
        responses: Mapping[str, HarnessToolResult] | None = None,
        documents: Mapping[str, object] | None = None,
    ) -> None:
        self._responses = dict(responses or {})
        self._documents = dict(documents or {})
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> HarnessToolResult:
        resolved = dict(arguments or {})
        self.calls.append((name, resolved))
        if name == TOOL_READ_ARTIFACT:
            document = self._documents.get(str(resolved.get("name")))
            if document is None:
                return HarnessToolResult(tool=name, ok=True, structured={})
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={"content": json.dumps(document), "encoding": "utf-8"},
            )
        if name == TOOL_LIST_ARTIFACTS:
            return HarnessToolResult(
                tool=name, ok=True, structured={"job_id": JOB_ID, "artifacts": []}
            )
        return self._responses[name]


def _not_found(tool: str) -> HarnessToolResult:
    return HarnessToolResult(
        tool=tool,
        ok=False,
        failure=HarnessToolFailure(
            kind="tool", message=f"job '{JOB_ID}' does not exist", tool=tool
        ),
    )


def _tool_job(
    tool_name: str,
    *,
    state: ToolJobState = ToolJobState.SUBMITTED,
    result: Mapping[str, object] | None = None,
    error_message: str | None = None,
) -> ToolJob:
    return ToolJob(
        workspace_id="workspace-one",
        created_at=NOW,
        created_by="operator",
        job_id=JOB_ID,
        execution_id="execution-1",
        chat_id="chat-1",
        tool_name=tool_name,
        submitted_by="operator",
        state=state,
        result=result,
        error_message=error_message,
        conversation_id="chat-1",
        root_conversation_id="chat-1",
        parent_conversation_id=None,
    )


def _lookup(job: ToolJob):
    def resolve(workspace_id: str, job_id: str) -> ToolJob | None:
        return job if job_id == job.job_id else None

    return resolve


def _coordinator(
    tools: FakeTools, job: ToolJob | None = None
) -> tuple[BacktestRunCoordinator, InMemoryBacktestStore]:
    store = InMemoryBacktestStore()
    service = BacktestDataService(store=store, clock=lambda: NOW)
    coordinator = BacktestRunCoordinator(
        service.store,
        tools,
        tracking=None if job is None else _lookup(job),
        clock=lambda: NOW,
    )
    return coordinator, store


def test_non_harness_job_is_never_observed_or_queried_as_a_harness_execution() -> None:
    tools = FakeTools()
    tracked = _tool_job("research", state=ToolJobState.RUNNING)
    coordinator, store = _coordinator(tools, tracked)

    for tool in ("run_backtest", TOOL_GET_JOB_RESULT):
        enrichment = coordinator.handle_tool_result(
            "workspace-one",
            HarnessToolResult(
                tool=tool,
                ok=True,
                structured={"job_id": JOB_ID, "state": "succeeded"},
            ),
        )
        assert enrichment is None

    assert tools.calls == []
    assert store.list("workspace-one") == ()


def test_non_harness_job_materializes_a_tracking_truth_not_a_harness_report() -> None:
    tools = FakeTools()
    tracked = _tool_job("research", state=ToolJobState.RUNNING)
    coordinator, store = _coordinator(tools, tracked)

    card = coordinator.materialize("workspace-one", JOB_ID)

    assert "status" not in card
    assert card["executionCertainty"] == "unknown_outcome"
    assert card["harness_execution"] is False
    assert card["state"] == "running"
    assert tools.calls == []
    assert store.list("workspace-one") == ()


def test_pre_dispatch_failure_stays_not_started_without_a_harness_failure() -> None:
    tools = FakeTools(
        {
            TOOL_GET_JOB_STATUS: _not_found(TOOL_GET_JOB_STATUS),
            TOOL_GET_JOB_RESULT: _not_found(TOOL_GET_JOB_RESULT),
        }
    )
    tracked = _tool_job(
        HARNESS_RESEARCH_MODEL_TOOL,
        state=ToolJobState.FAILED,
        result={"executionCertainty": "not_started"},
        error_message="model publication failed before dispatch",
    )
    coordinator, store = _coordinator(tools, tracked)

    card = coordinator.materialize("workspace-one", JOB_ID)

    assert "status" not in card
    assert card["executionCertainty"] == "not_started"
    assert card["harness_execution"] is True
    assert card["error"] == "model publication failed before dispatch"
    assert store.list("workspace-one") == ()


def test_harness_job_is_observed_and_materialized() -> None:
    tools = FakeTools(
        {
            TOOL_GET_JOB_STATUS: HarnessToolResult(
                tool=TOOL_GET_JOB_STATUS,
                ok=True,
                structured={"job_id": JOB_ID, "state": "succeeded"},
            ),
            TOOL_GET_JOB_RESULT: HarnessToolResult(
                tool=TOOL_GET_JOB_RESULT,
                ok=True,
                structured={
                    "job_id": JOB_ID,
                    "kind": "backtest",
                    "state": "succeeded",
                    "strategy_id": "momentum",
                },
            ),
        },
        documents=ARTIFACTS,
    )
    tracked = _tool_job("run_backtest", state=ToolJobState.RUNNING)
    coordinator, store = _coordinator(tools, tracked)

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

    terminal = coordinator.handle_tool_result(
        "workspace-one",
        HarnessToolResult(
            tool=TOOL_GET_JOB_RESULT,
            ok=True,
            structured={"job_id": JOB_ID, "state": "succeeded"},
        ),
    )
    assert terminal is not None
    records = store.list("workspace-one")
    assert len(records) == 1
    assert records[0].job_id == JOB_ID


def test_harness_observed_terminal_failure_still_reports_failed() -> None:
    tools = FakeTools(
        {
            TOOL_GET_JOB_STATUS: HarnessToolResult(
                tool=TOOL_GET_JOB_STATUS,
                ok=True,
                structured={"job_id": JOB_ID, "state": "failed"},
            )
        }
    )
    tracked = _tool_job("run_backtest", state=ToolJobState.FAILED)
    coordinator, store = _coordinator(tools, tracked)

    card = coordinator.materialize("workspace-one", JOB_ID)

    assert card == {"job_id": JOB_ID, "state": "failed"}
    assert store.list("workspace-one") == ()
