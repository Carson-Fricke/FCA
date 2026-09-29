"""Screener service lifecycle tests over a fake MCP tool provider."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from thesis.screening import (
    InMemoryScreenerStore,
    ScreenerService,
    ScreenerUnavailableError,
    ScreeningArtifactError,
    ScreeningRecord,
    ScreeningRunError,
    ScreenRow,
)

WORKSPACE = "alpha-lab"
RUN_ID = "scr-run-001"
AS_OF = "2026-09-28"


def _service(provider, *, max_polls: int = 240) -> tuple[ScreenerService, list[float]]:
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)

    return (
        ScreenerService(
            tools=provider,
            sleep=sleep,
            poll_interval_seconds=0.01,
            max_polls=max_polls,
        ),
        slept,
    )


def test_save_and_list_definitions_are_content_addressed_and_ordered(
    definition_factory,
) -> None:
    store = InMemoryScreenerStore()
    service = ScreenerService(store=store)
    first = definition_factory(name="A screen")
    second = definition_factory(name="B screen", rank_metric="value")

    saved = service.save_definition(WORKSPACE, first)
    assert saved.definition_id == first.definition_id
    service.save_definition(WORKSPACE, first)

    listed = service.list_definitions(WORKSPACE)
    assert listed == (first,)
    assert service.list_definitions("other-workspace") == ()

    service.save_definition(WORKSPACE, second)
    assert service.list_definitions(WORKSPACE) == tuple(
        sorted((first, second), key=lambda item: item.definition_id)
    )


def test_run_launches_polls_and_imports_the_record(
    fake_provider, definition_factory
) -> None:
    definition = definition_factory()
    provider = fake_provider(states=("running", "running", "complete"))
    service, slept = _service(provider)
    service.save_definition(WORKSPACE, definition)

    record = service.run(WORKSPACE, definition.definition_id, AS_OF)

    assert provider.tool_names() == (
        "run_screener",
        "get_screener_status",
        "get_screener_status",
        "get_screener_status",
        "get_run",
        "list_artifacts",
        "read_artifact",
    )
    launch = provider.configs[0]
    assert launch["as_of"] == AS_OF
    assert launch["bundle_hash"] == definition.source_sha256
    assert launch["definition_id"] == definition.definition_id
    assert provider.calls[0][1]["workspace"] == WORKSPACE
    assert slept == [0.01, 0.01]

    assert record.run_id == RUN_ID
    assert record.workspace_id == WORKSPACE
    assert record.definition_id == definition.definition_id
    assert record.definition_hash == definition.definition_sha256
    assert record.status == "complete"
    assert record.as_of == AS_OF
    assert [row.symbol for row in record.rows] == ["AAPL", "MSFT"]
    assert record.rows[0].metrics == {"momentum_12_1": "0.44"}
    artifact = record.provenance["artifact"]
    catalog_entry = record.provenance["catalog"]
    assert isinstance(artifact, Mapping)
    assert isinstance(catalog_entry, Mapping)
    assert artifact["name"] == "screen.json"
    assert catalog_entry["run_id"] == RUN_ID

    assert service.get_result(WORKSPACE, record.result_id) is record
    assert service.list_results(WORKSPACE) == (record,)
    document = record.document()
    rows = document["rows"]
    assert isinstance(rows, list)
    assert isinstance(rows[0], Mapping)
    assert document["result_id"] == record.result_id
    assert rows[0]["symbol"] == "AAPL"


def test_succeeded_job_state_is_terminal(fake_provider, definition_factory) -> None:
    definition = definition_factory()
    provider = fake_provider(states=("running", "succeeded"))
    service, slept = _service(provider)
    service.save_definition(WORKSPACE, definition)

    record = service.run(WORKSPACE, definition.definition_id, AS_OF)

    assert record.status == "complete"
    assert slept == [0.01]


def test_status_tool_state_alias_fails_closed(
    fake_provider, definition_factory
) -> None:
    definition = definition_factory()
    provider = fake_provider(states=("complete",), status_alias=True)
    service, _ = _service(provider)
    service.save_definition(WORKSPACE, definition)

    with pytest.raises(ScreeningRunError, match="no state"):
        service.run(WORKSPACE, definition.definition_id, AS_OF)


def test_run_without_a_boundary_fails_closed(definition_factory) -> None:
    definition = definition_factory()
    service, _ = _service(None)
    service.save_definition(WORKSPACE, definition)

    with pytest.raises(ScreenerUnavailableError):
        service.run(WORKSPACE, definition.definition_id, AS_OF)

    assert service.list_results(WORKSPACE) == ()


def test_launch_failure_is_unavailable_and_stores_nothing(
    fake_provider, definition_factory
) -> None:
    definition = definition_factory()
    service, _ = _service(fake_provider(launch_error=True))
    service.save_definition(WORKSPACE, definition)

    with pytest.raises(ScreenerUnavailableError):
        service.run(WORKSPACE, definition.definition_id, AS_OF)

    assert service.list_results(WORKSPACE) == ()


def test_status_failure_is_unavailable(fake_provider, definition_factory) -> None:
    definition = definition_factory()
    service, _ = _service(fake_provider(states=("running",), status_error=True))
    service.save_definition(WORKSPACE, definition)

    with pytest.raises(ScreenerUnavailableError):
        service.run(WORKSPACE, definition.definition_id, AS_OF)


def test_terminal_failure_stores_nothing(fake_provider, definition_factory) -> None:
    definition = definition_factory()
    service, _ = _service(fake_provider(states=("running", "failed")))
    service.save_definition(WORKSPACE, definition)

    with pytest.raises(ScreeningRunError, match="failed") as failure:
        service.run(WORKSPACE, definition.definition_id, AS_OF)

    assert not isinstance(failure.value, ScreenerUnavailableError)
    assert service.list_results(WORKSPACE) == ()


def test_status_polling_is_bounded(fake_provider, definition_factory) -> None:
    definition = definition_factory()
    provider = fake_provider(states=("running", "running", "running"))
    service, slept = _service(provider, max_polls=2)
    service.save_definition(WORKSPACE, definition)

    with pytest.raises(ScreeningRunError, match="terminal"):
        service.run(WORKSPACE, definition.definition_id, AS_OF)

    assert provider.tool_names() == (
        "run_screener",
        "get_screener_status",
        "get_screener_status",
    )
    assert slept == [0.01]
    assert service.list_results(WORKSPACE) == ()


def test_unknown_definition_is_a_key_error() -> None:
    service, _ = _service(None)
    with pytest.raises(KeyError):
        service.run(WORKSPACE, "sha256:missing", AS_OF)


def test_missing_artifact_fails_closed(fake_provider, definition_factory) -> None:
    definition = definition_factory()
    service, _ = _service(fake_provider(states=("complete",), artifact_present=False))
    service.save_definition(WORKSPACE, definition)

    with pytest.raises(ScreeningArtifactError):
        service.run(WORKSPACE, definition.definition_id, AS_OF)

    assert service.list_results(WORKSPACE) == ()


def test_unreadable_artifact_fails_closed(fake_provider, definition_factory) -> None:
    definition = definition_factory()
    service, _ = _service(fake_provider(states=("complete",), artifact_readable=False))
    service.save_definition(WORKSPACE, definition)

    with pytest.raises(ScreeningArtifactError):
        service.run(WORKSPACE, definition.definition_id, AS_OF)


def test_unknown_result_is_a_key_error() -> None:
    service, _ = _service(None)
    with pytest.raises(KeyError):
        service.get_result(WORKSPACE, "sha256:missing")


def _record(result_id: str) -> ScreeningRecord:
    return ScreeningRecord(
        result_id=result_id,
        workspace_id=WORKSPACE,
        definition_id="sha256:" + "a" * 64,
        definition_hash="a" * 64,
        run_id="run-" + result_id,
        status="complete",
        as_of=AS_OF,
        rows=(ScreenRow(symbol="AAPL", metrics={"momentum": "0.4"}),),
        provenance={"run_id": "run-" + result_id},
    )


def test_store_orders_records_by_result_id() -> None:
    store = InMemoryScreenerStore()
    later = _record("sha256:" + "b" * 64)
    earlier = _record("sha256:" + "a" * 64)
    store.add_record(WORKSPACE, later)
    store.add_record(WORKSPACE, earlier)
    store.add_record(WORKSPACE, later)

    assert store.list_records(WORKSPACE) == (earlier, later)
    assert store.get_record(WORKSPACE, later.result_id) is later
    assert store.get_record(WORKSPACE, "sha256:" + "c" * 64) is None
    assert store.list_records("other-workspace") == ()
