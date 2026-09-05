"""Focused tests for the lake run-record model, mappers, and repositories."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, cast

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, TableEntity, UpdateMode

from lake.catalog import (
    AzureTableRunRepository,
    InMemoryRunRepository,
    RunRecord,
    RunState,
    run_from_entity,
    run_to_entity,
)

_METRICS_JSON = '{"max_drawdown":"-0.0825","sharpe":"1.2345"}'


class FakeTable:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], TableEntity] = {}
        self.queries: list[tuple[str, dict[str, object] | None]] = []
        self.deletes: list[tuple[str, str]] = []

    def get_entity(self, partition_key: str, row_key: str) -> TableEntity:
        try:
            return self.entities[(partition_key, row_key)]
        except KeyError:
            raise ResourceNotFoundError("missing") from None

    def upsert_entity(self, entity: TableEntity, mode: UpdateMode) -> None:
        assert mode is UpdateMode.REPLACE
        self.entities[(entity["PartitionKey"], entity["RowKey"])] = TableEntity(entity)

    def query_entities(
        self,
        query_filter: str,
        *,
        parameters: dict[str, object] | None = None,
    ) -> list[TableEntity]:
        self.queries.append((query_filter, parameters))
        partition = (parameters or {}).get("partition")
        return [
            entity
            for entity in self.entities.values()
            if entity["PartitionKey"] == partition
        ]

    def delete_entity(self, partition_key: str, row_key: str) -> None:
        self.deletes.append((partition_key, row_key))
        self.entities.pop((partition_key, row_key), None)


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


@pytest.mark.parametrize("status", list(RunState))
def test_run_records_round_trip_through_entity(status: RunState) -> None:
    record = _record(status)
    entity = run_to_entity(record)

    assert entity["PartitionKey"] == "runs"
    assert entity["RowKey"] == "run-2024-01-02"
    assert entity["Metrics"] == _METRICS_JSON
    assert run_from_entity(entity) == record


def test_run_entity_omits_optional_fields_and_encodes_row_keys() -> None:
    record = RunRecord(
        run_id="run/2024 01",
        status=RunState.FAILED,
        strategy_id="mean-revert",
    )
    entity = run_to_entity(record)

    assert entity["RowKey"] == "run%2F2024%2001"
    assert entity["Metrics"] == "{}"
    for name in ("DataStart", "DataEnd", "RunDigest", "ConfigurationDigest"):
        assert name not in entity
    assert run_from_entity(entity) == record


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


def test_azure_run_repository_round_trips_without_live_azure() -> None:
    table = FakeTable()
    adapter: AzureTableRunRepository = AzureTableRunRepository(cast(TableClient, table))
    record = _record(RunState.FAILED)

    assert adapter.upsert(record) == record
    assert ("runs", "run-2024-01-02") in table.entities
    assert adapter.get(record.run_id) == record
    assert adapter.list(status=RunState.FAILED) == (record,)
    assert adapter.list(status=RunState.COMPLETE) == ()
    assert adapter.list(strategy_id="momentum") == (record,)
    assert adapter.list(strategy_id="mean-revert") == ()
    assert adapter.delete(record.run_id) is True
    assert ("runs", "run-2024-01-02") in table.deletes
    assert adapter.delete(record.run_id) is False
    assert adapter.get(record.run_id) is None


def test_azure_run_repository_queries_only_the_runs_partition() -> None:
    table = FakeTable()
    adapter = AzureTableRunRepository(cast(TableClient, table))
    record = _record()

    adapter.upsert(record)

    assert adapter.list() == (record,)
    assert table.queries == [("PartitionKey eq @partition", {"partition": "runs"})]
    assert adapter.get("missing-run") is None
