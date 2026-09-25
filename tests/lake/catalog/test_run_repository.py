"""Focused tests for the lake run-record model and repository."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, cast

import pytest

from lake.catalog import InMemoryRunRepository, RunRecord, RunState


def _record(
    status: RunState = RunState.COMPLETE,
    run_id: str = "run-2024-01-02",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        status=status,
        strategy_id="momentum",
        data_start=date(2024, 1, 2),
        data_end=date(2024, 1, 3),
        run_digest="digest-1",
        configuration_digest="config-1",
        artifact_count=2,
        total_bytes=24,
        metrics={"max_drawdown": "-0.0825", "sharpe": "1.2345"},
    )


def test_run_records_round_trip_through_memory() -> None:
    record = _record()
    repository = InMemoryRunRepository()

    repository.upsert(record)

    assert repository.get(record.run_id) == record


def test_run_state_coerces_strings_and_rejects_unknown_values() -> None:
    assert RunState("complete") is RunState.COMPLETE
    with pytest.raises(ValueError):
        RunState("bogus")


def test_run_record_validation_rejects_invalid_rows() -> None:
    with pytest.raises(ValueError, match="run_id must not be empty"):
        RunRecord(run_id="  ", status=RunState.COMPLETE, strategy_id="momentum")
    with pytest.raises(ValueError, match="strategy_id must not be empty"):
        RunRecord(run_id="run-1", status=RunState.COMPLETE, strategy_id="  ")
    with pytest.raises(TypeError, match="must be dates, not datetimes"):
        RunRecord(
            run_id="run-1",
            status=RunState.COMPLETE,
            strategy_id="momentum",
            data_start=datetime(2024, 1, 2),
        )
    with pytest.raises(ValueError, match="on or before data_end"):
        RunRecord(
            run_id="run-1",
            status=RunState.COMPLETE,
            strategy_id="momentum",
            data_start=date(2024, 1, 3),
            data_end=date(2024, 1, 2),
        )
    with pytest.raises(ValueError, match="must not be negative"):
        RunRecord(
            run_id="run-1",
            status=RunState.COMPLETE,
            strategy_id="momentum",
            artifact_count=-1,
        )
    with pytest.raises(TypeError, match="must be strings"):
        RunRecord(
            run_id="run-1",
            status=RunState.COMPLETE,
            strategy_id="momentum",
            metrics=cast("Mapping[str, str]", {"sharpe": 1.25}),
        )
    with pytest.raises(TypeError, match="must be a mapping"):
        RunRecord(
            run_id="run-1",
            status=RunState.COMPLETE,
            strategy_id="momentum",
            metrics=cast("Any", ("sharpe", "1.25")),
        )


def test_memory_run_repository_upserts_filters_sorts_and_deletes() -> None:
    complete = _record()
    failed = _record(RunState.FAILED, "run-2024-01-01")
    repository = InMemoryRunRepository([complete])

    assert repository.get(" run-2024-01-02 ") == complete
    assert repository.upsert(failed) == failed
    assert repository.put(complete) == complete
    assert [row.run_id for row in repository.list()] == [
        "run-2024-01-01",
        "run-2024-01-02",
    ]
    assert repository.list(status=RunState.COMPLETE, strategy_id="momentum") == (
        complete,
    )
    assert repository.list(status=RunState.FAILED, strategy_id="momentum") == (failed,)
    assert repository.list(status=RunState.FAILED, strategy_id="mean-revert") == ()
    assert repository.list(strategy_id="momentum") == (failed, complete)
    assert repository.delete("run-2024-01-01") is True
    assert repository.delete("run-2024-01-01") is False
    assert repository.get("run-2024-01-01") is None
