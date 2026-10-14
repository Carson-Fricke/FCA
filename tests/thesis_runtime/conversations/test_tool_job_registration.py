"""Repeated tool-job registration joins by identity and attribution."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count
from typing import Any, cast

import pytest

from thesis_runtime.contracts.codec import record_from_payload, record_payload
from thesis_runtime.contracts.records import (
    ChatContractError,
    ToolJob,
    ToolJobConflictError,
    ToolJobState,
    WorkflowDefinition,
    WorkflowExecution,
)
from thesis_runtime.conversations.service import ChatService

WORKSPACE = "workspace-a"
ATTRIBUTION = {"configSha256": "a" * 64}


def _service() -> ChatService:
    ticks = count(1)
    ids = count(1)
    return ChatService(
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC).replace(microsecond=next(ticks)),
        id_factory=lambda prefix: f"{prefix}-{next(ids)}",
    )


def _execution(service: ChatService) -> WorkflowExecution:
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    return service.launch_workflow(
        WORKSPACE,
        chat.chat_id,
        workflow=WorkflowDefinition("research", "1.0.0", "Research", ("creation",)),
        actor_id="operator",
        body="Start research",
    )


def _register(service: ChatService, execution: WorkflowExecution) -> None:
    service.submit_tool_job(
        WORKSPACE,
        execution.execution_id,
        tool_name="run_backtest",
        actor_id="operator",
        job_id="job-fixed",
        attribution=ATTRIBUTION,
    )


def test_repeated_registration_joins_without_reset_or_overwrite() -> None:
    service = _service()
    execution = _execution(service)
    _register(service, execution)
    service.start_tool_job(WORKSPACE, "job-fixed", actor_id="operator")
    service.record_tool_result(
        WORKSPACE, "job-fixed", result={"state": "succeeded"}, success=True
    )
    existing = service.get_tool_job(WORKSPACE, "job-fixed")

    joined = service.submit_tool_job(
        WORKSPACE,
        execution.execution_id,
        tool_name="run_backtest",
        actor_id="operator",
        job_id="job-fixed",
        attribution=ATTRIBUTION,
    )

    assert joined == existing
    assert joined.state is ToolJobState.SUCCEEDED
    assert joined.result == {"state": "succeeded"}
    assert joined.updated_at == existing.updated_at
    assert joined.created_at == existing.created_at
    assert service.get_execution(WORKSPACE, execution.execution_id).job_ids == (
        "job-fixed",
    )
    assert service.get_chat(WORKSPACE, execution.chat_id).job_ids == ("job-fixed",)
    assert len(service.persistence.list(ToolJob, WORKSPACE)) == 1


@pytest.mark.parametrize(
    "change", ["execution", "tool", "attribution", "investigation"]
)
def test_mismatched_registration_is_an_explicit_conflict(change: str) -> None:
    service = _service()
    execution = _execution(service)
    _register(service, execution)
    service.start_tool_job(WORKSPACE, "job-fixed", actor_id="operator")
    before = service.get_tool_job(WORKSPACE, "job-fixed")

    target = execution.execution_id
    tool_name = "run_backtest"
    attribution = dict(ATTRIBUTION)
    investigation_id = None
    if change == "execution":
        target = _execution(service).execution_id
    elif change == "tool":
        tool_name = "run_study"
    elif change == "attribution":
        attribution = {"configSha256": "b" * 64}
    else:
        investigation_id = "other-resource"

    with pytest.raises(ToolJobConflictError):
        service.submit_tool_job(
            WORKSPACE,
            target,
            tool_name=tool_name,
            actor_id="operator",
            job_id="job-fixed",
            attribution=attribution,
            references={
                "fca.investigation": (
                    () if (investigation_id) is None else ((investigation_id),)
                )
            },
        )

    assert service.get_tool_job(WORKSPACE, "job-fixed") == before
    assert service.get_execution(WORKSPACE, execution.execution_id).job_ids == (
        "job-fixed",
    )


def test_ambiguous_attribution_matches_fail_closed() -> None:
    service = _service()
    execution = _execution(service)
    for job_id in ("job-one", "job-two"):
        service.submit_tool_job(
            WORKSPACE,
            execution.execution_id,
            tool_name="run_backtest",
            actor_id="operator",
            job_id=job_id,
            attribution=ATTRIBUTION,
        )

    with pytest.raises(ToolJobConflictError, match="identical attribution"):
        service.find_execution_tool_job(
            WORKSPACE,
            execution.execution_id,
            tool_name="run_backtest",
            attribution=ATTRIBUTION,
        )


def test_attribution_is_immutable_and_round_trips_strictly() -> None:
    service = _service()
    execution = _execution(service)
    job = service.submit_tool_job(
        WORKSPACE,
        execution.execution_id,
        tool_name="run_backtest",
        actor_id="operator",
        job_id="job-fixed",
        attribution={"configSha256": "a" * 64, "revision": 2},
    )

    with pytest.raises(TypeError):
        cast(Any, job.attribution)["revision"] = 3

    payload = record_payload(job)
    assert payload["attribution"] == {"configSha256": "a" * 64, "revision": 2}
    assert record_from_payload(ToolJob, payload) == job
    with pytest.raises(ChatContractError, match="outdated"):
        record_from_payload(
            ToolJob,
            {key: value for key, value in payload.items() if key != "attribution"},
        )
    with pytest.raises(ValueError):
        record_from_payload(ToolJob, {**payload, "attribution": ["bad"]})
    with pytest.raises(ChatContractError):
        record_from_payload(ToolJob, {**payload, "attribution": {"key": ["bad"]}})
