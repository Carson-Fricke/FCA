"""Tracking classification: harness executions versus generic tool jobs."""

from collections.abc import Mapping
from datetime import UTC, datetime

from thesis.chat.contracts import (
    HARNESS_RESEARCH_MODEL_TOOL,
    ToolJob,
    ToolJobState,
    is_harness_execution_tool,
)
from thesis.chat.service import ChatService
from thesis.integrations.mcp import (
    TOOL_LAUNCH_TRADER,
    TOOL_RUN_BACKTEST,
    TOOL_RUN_SCREENER,
    TOOL_RUN_STUDY,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _job(
    tool_name: str,
    *,
    state: ToolJobState = ToolJobState.SUBMITTED,
    result: Mapping[str, object] | None = None,
) -> ToolJob:
    return ToolJob(
        workspace_id="workspace-one",
        created_at=NOW,
        created_by="operator",
        job_id="job-1",
        execution_id="execution-1",
        chat_id="chat-1",
        tool_name=tool_name,
        submitted_by="operator",
        state=state,
        result=result,
    )


def test_harness_execution_operations_are_classified_harness_backed() -> None:
    for name in (
        TOOL_RUN_BACKTEST,
        TOOL_RUN_STUDY,
        TOOL_RUN_SCREENER,
        TOOL_LAUNCH_TRADER,
        HARNESS_RESEARCH_MODEL_TOOL,
    ):
        assert is_harness_execution_tool(name)
        assert _job(name).is_harness_execution


def test_generic_operations_are_not_harness_executions() -> None:
    for name in ("research", "search_workspace", "get_job_result", "read_artifact"):
        assert not is_harness_execution_tool(name)
        assert not _job(name).is_harness_execution


def test_submission_certainty_never_claims_a_failed_harness_execution() -> None:
    assert _job("research").submission_certainty == "not_started"
    assert (
        _job("research", state=ToolJobState.FAILED).submission_certainty
        == "unknown_outcome"
    )
    declared = _job(
        "research",
        state=ToolJobState.FAILED,
        result={"executionCertainty": "not_started"},
    )
    assert declared.submission_certainty == "not_started"
    observed = _job(
        HARNESS_RESEARCH_MODEL_TOOL,
        state=ToolJobState.FAILED,
        result={"executionCertainty": "observed_failure"},
    )
    assert observed.submission_certainty == "observed_failure"


def test_find_tool_job_is_the_non_raising_tracking_lookup() -> None:
    service = ChatService()
    job = _job("run_backtest")
    service.persistence.save(job)

    assert service.find_tool_job("workspace-one", "job-1") is job
    assert service.find_tool_job("workspace-one", "job-missing") is None
