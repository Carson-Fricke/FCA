"""Importer tests: one terminal run and its frozen screen.json artifact.

Every accepted document matches the frozen ``fca-harness-screen-v1`` schema
exactly; every old or aliased shape the importer used to tolerate is asserted
to fail closed.
"""

from __future__ import annotations

import pytest

from thesis.screening import (
    ScreeningArtifactError,
    ScreeningRecord,
    ScreeningRunError,
    ScreeningRunPending,
    import_screening_run,
)

WORKSPACE = "alpha-lab"
RUN_ID = "scr-run-001"
AS_OF = "2026-09-28"


def _import(provider, definition, **overrides: object) -> ScreeningRecord:
    values: dict[str, object] = {
        "tools": provider,
        "workspace_id": WORKSPACE,
        "definition": definition,
        "run_id": RUN_ID,
        "as_of": AS_OF,
    }
    values.update(overrides)
    return import_screening_run(**values)  # type: ignore[arg-type]


def test_terminal_run_with_screen_artifact_builds_the_record(
    fake_provider, definition_factory
) -> None:
    definition = definition_factory()
    provider = fake_provider()

    record = _import(provider, definition)

    assert record.workspace_id == WORKSPACE
    assert record.run_id == RUN_ID
    assert record.status == "complete"
    assert record.as_of == AS_OF
    assert record.definition_id == definition.definition_id
    assert record.definition_hash == definition.definition_sha256
    assert [row.symbol for row in record.rows] == ["AAPL", "MSFT"]
    assert record.rows[1].metrics == {"momentum_12_1": "0.31"}
    assert record.result_id.startswith("sha256:")
    assert provider.tool_names() == ("get_run", "list_artifacts", "read_artifact")


def test_import_is_deterministic(fake_provider, definition_factory) -> None:
    definition = definition_factory()
    first = _import(fake_provider(), definition)
    second = _import(fake_provider(), definition)

    assert first.result_id == second.result_id
    assert first.document() == second.document()


def test_hex_encoded_artifact_is_decoded(fake_provider, definition_factory) -> None:
    record = _import(fake_provider(artifact_encoding="hex"), definition_factory())
    assert [row.symbol for row in record.rows] == ["AAPL", "MSFT"]


def test_wrong_schema_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(schema="fca-harness-screen-result-v1")
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="schema"):
        _import(provider, definition_factory())


def test_missing_schema_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory()
    del document["schema"]
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="schema"):
        _import(provider, definition_factory())


def test_as_of_alias_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory()
    document["asOf"] = document.pop("as_of")
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="as_of"):
        _import(provider, definition_factory())


def test_missing_as_of_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory()
    del document["as_of"]
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="as_of"):
        _import(provider, definition_factory())


def test_artifact_as_of_is_the_record_as_of(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(as_of="2026-09-25")
    provider = fake_provider(screen_document=document)

    record = _import(provider, definition_factory())

    assert record.as_of == "2026-09-25"


def test_row_rank_key_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(
        rows=[{"symbol": "AAPL", "rank": 1, "metrics": {"momentum_12_1": "0.44"}}]
    )
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="rank"):
        _import(provider, definition_factory())


def test_unexpected_row_key_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(
        rows=[{"symbol": "AAPL", "metrics": {}, "decision": "buy"}]
    )
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="decision"):
        _import(provider, definition_factory())


def test_missing_rows_fail_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory()
    del document["rows"]
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="row list"):
        _import(provider, definition_factory())


def test_missing_row_symbol_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(rows=[{"metrics": {"momentum": "0.4"}}])
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="symbol"):
        _import(provider, definition_factory())


def test_empty_row_symbol_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(rows=[{"symbol": "  ", "metrics": {}}])
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="row symbol"):
        _import(provider, definition_factory())


def test_missing_row_metrics_fail_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(rows=[{"symbol": "AAPL"}])
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="metrics"):
        _import(provider, definition_factory())


def test_non_string_metric_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(
        rows=[{"symbol": "AAPL", "metrics": {"momentum": 0.44}}]
    )
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="decimal string"):
        _import(provider, definition_factory())


def test_nested_metric_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(
        rows=[{"symbol": "AAPL", "metrics": {"momentum": {"nested": "1"}}}]
    )
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="decimal string"):
        _import(provider, definition_factory())


def test_non_numeric_metric_string_is_exact_text(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(
        rows=[{"symbol": "AAPL", "metrics": {"label": "fixture"}}]
    )
    provider = fake_provider(screen_document=document)

    record = _import(provider, definition_factory())

    assert record.rows[0].metrics == {"label": "fixture"}


def test_definition_id_mismatch_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(definition_id="sha256:" + "f" * 64)
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="different definition"):
        _import(provider, definition_factory())


def test_definition_hash_mismatch_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(definition_hash="f" * 64)
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="different definition"):
        _import(provider, definition_factory())


def test_bundle_hash_mismatch_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(bundle_hash="f" * 64)
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="different definition"):
        _import(provider, definition_factory())


def test_missing_bundle_hash_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory()
    del document["bundle_hash"]
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="bundle_hash"):
        _import(provider, definition_factory())


def test_matching_definition_echoes_are_accepted(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    definition = definition_factory()
    document = screen_document_factory()
    record = _import(fake_provider(screen_document=document), definition)
    assert record.definition_id == definition.definition_id
    assert record.definition_hash == definition.definition_sha256


def test_failed_artifact_surfaces_its_error(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(status="failed", error="lake unavailable")
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningRunError, match="lake unavailable"):
        _import(provider, definition_factory())


def test_failed_artifact_without_error_still_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(status="failed")
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningRunError, match="failed"):
        _import(provider, definition_factory())


def test_artifact_status_alias_fails_closed(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    document = screen_document_factory(status="succeeded")
    provider = fake_provider(screen_document=document)

    with pytest.raises(ScreeningArtifactError, match="invalid status"):
        _import(provider, definition_factory())


def test_pending_catalog_raises_pending(fake_provider, definition_factory) -> None:
    provider = fake_provider(catalog={"run_id": RUN_ID, "status": "running"})

    with pytest.raises(ScreeningRunPending):
        _import(provider, definition_factory())

    assert provider.tool_names() == ("get_run",)


def test_failed_catalog_surfaces_the_artifact_error(
    fake_provider, definition_factory, screen_document_factory
) -> None:
    provider = fake_provider(
        catalog={"run_id": RUN_ID, "status": "failed"},
        screen_document=screen_document_factory(status="failed", error="no coverage"),
    )

    with pytest.raises(ScreeningRunError, match="no coverage"):
        _import(provider, definition_factory())


def test_failed_catalog_without_an_artifact_fails_closed(
    fake_provider, definition_factory
) -> None:
    provider = fake_provider(
        catalog={"run_id": RUN_ID, "status": "failed"}, artifact_present=False
    )

    with pytest.raises(ScreeningRunError, match="failed"):
        _import(provider, definition_factory())


def test_catalog_state_alias_fails_closed(fake_provider, definition_factory) -> None:
    provider = fake_provider(catalog={"run_id": RUN_ID, "state": "failed"})

    with pytest.raises(ScreeningRunError, match="no status"):
        _import(provider, definition_factory())


def test_catalog_without_status_fails_closed(fake_provider, definition_factory) -> None:
    provider = fake_provider(catalog={"run_id": RUN_ID})

    with pytest.raises(ScreeningRunError, match="no status"):
        _import(provider, definition_factory())


@pytest.mark.parametrize("status", ["completed", "succeeded", "success"])
def test_catalog_terminal_alias_fails_closed(
    fake_provider, definition_factory, status: str
) -> None:
    provider = fake_provider(catalog={"run_id": RUN_ID, "status": status})

    with pytest.raises(ScreeningRunError, match="no usable terminal status"):
        _import(provider, definition_factory())


def test_missing_screen_artifact_fails_closed(
    fake_provider, definition_factory
) -> None:
    provider = fake_provider(artifact_present=False)
    with pytest.raises(ScreeningArtifactError, match="screen.json"):
        _import(provider, definition_factory())
    assert provider.tool_names() == ("get_run", "list_artifacts")


def test_unreadable_artifact_fails_closed(fake_provider, definition_factory) -> None:
    provider = fake_provider(artifact_readable=False)
    with pytest.raises(ScreeningArtifactError, match="read_artifact"):
        _import(provider, definition_factory())


def test_malformed_artifact_fails_closed(fake_provider, definition_factory) -> None:
    provider = fake_provider(artifact_content="{not json")
    with pytest.raises(ScreeningArtifactError, match="unreadable"):
        _import(provider, definition_factory())
