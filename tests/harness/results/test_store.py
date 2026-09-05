"""Focused tests for the backtest results store and its run catalog."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import date, datetime
from typing import Final, cast

import pytest

from harness.configuration.hashing import (
    canonical_configuration_bytes,
    configuration_hash,
)
from harness.results.store import (
    BacktestResultsStore,
    InMemoryRunBlobStore,
    InMemoryRunCatalogRepository,
    LakeRunCatalogRepository,
    RunBlobStore,
    RunCatalogRecord,
    RunCatalogRepository,
    RunStatus,
    StoredRun,
    artifact_digest,
    run_digest,
    run_prefix,
)
from lake.catalog import InMemoryRunRepository
from lake.storage import ReadableBlobStore

RUN_ID: Final = "run-2024-01-02"


def make_record(
    run_id: str = RUN_ID,
    *,
    status: RunStatus = RunStatus.COMPLETE,
    strategy_id: str = "momentum",
) -> RunCatalogRecord:
    """Build one catalog row with a deterministic data range and metrics."""
    return RunCatalogRecord(
        run_id=run_id,
        status=status,
        strategy_id=strategy_id,
        data_start=date(2024, 1, 2),
        data_end=date(2024, 1, 3),
        metrics={"sharpe": "1.2345", "max_drawdown": "-0.0825"},
    )


def make_artifacts() -> dict[str, bytes]:
    """Return the canonical two-artifact upload for one run."""
    return {"actions.parquet": b"actions-bytes", "fills.parquet": b"fills-bytes"}


def make_store() -> tuple[
    BacktestResultsStore, InMemoryRunBlobStore, InMemoryRunCatalogRepository
]:
    """Build one store wired to fresh in-memory adapters."""
    blobs = InMemoryRunBlobStore()
    catalog = InMemoryRunCatalogRepository()
    return BacktestResultsStore(blobs, catalog), blobs, catalog


def test_put_writes_artifacts_under_run_prefix_and_upserts_row() -> None:
    store, blobs, _ = make_store()

    stored = store.put(make_record(), make_artifacts())

    prefix = run_prefix(RUN_ID)
    assert blobs.list_prefix(prefix) == (
        f"{prefix}/actions.parquet",
        f"{prefix}/fills.parquet",
    )
    assert store.get(RUN_ID) == stored
    assert stored.artifact_count == 2
    assert stored.total_bytes == len(b"actions-bytes") + len(b"fills-bytes")
    assert stored.status is RunStatus.COMPLETE
    assert stored.strategy_id == "momentum"
    assert stored.data_start == date(2024, 1, 2)
    assert stored.data_end == date(2024, 1, 3)
    assert stored.metrics["sharpe"] == "1.2345"


def test_fetch_round_trips_record_and_artifact_bytes() -> None:
    store, _, _ = make_store()
    uploaded = store.put(make_record(), make_artifacts())

    fetched = store.fetch(RUN_ID)

    assert isinstance(fetched, StoredRun)
    assert fetched.record == uploaded
    assert dict(fetched.artifacts) == make_artifacts()


def test_fetch_missing_run_raises_key_error() -> None:
    store, _, _ = make_store()

    with pytest.raises(KeyError, match="run not found"):
        store.fetch("missing-run")


def test_get_missing_run_returns_none() -> None:
    store, _, _ = make_store()

    assert store.get("missing-run") is None


def test_list_filters_by_status_and_strategy() -> None:
    store, _, _ = make_store()
    store.put(make_record("run-a"), make_artifacts())
    store.put(make_record("run-b", strategy_id="mean-revert"), make_artifacts())
    store.put(make_record("run-c", status=RunStatus.FAILED), make_artifacts())

    assert tuple(row.run_id for row in store.list()) == ("run-a", "run-b", "run-c")
    assert tuple(row.run_id for row in store.list(status=RunStatus.COMPLETE)) == (
        "run-a",
        "run-b",
    )
    assert tuple(row.run_id for row in store.list(strategy_id="momentum")) == (
        "run-a",
        "run-c",
    )
    assert tuple(
        row.run_id
        for row in store.list(status=RunStatus.COMPLETE, strategy_id="mean-revert")
    ) == ("run-b",)
    assert store.list(status=RunStatus.RUNNING) == ()


def test_put_replaces_the_whole_run_prefix() -> None:
    store, blobs, _ = make_store()
    store.put(make_record(), make_artifacts())

    store.put(make_record(), {"fills.parquet": b"fills-bytes-2"})

    prefix = run_prefix(RUN_ID)
    assert blobs.list_prefix(prefix) == (f"{prefix}/fills.parquet",)
    assert dict(store.fetch(RUN_ID).artifacts) == {"fills.parquet": b"fills-bytes-2"}


def test_delete_removes_blob_prefix_and_catalog_row_together() -> None:
    store, blobs, catalog = make_store()
    store.put(make_record("run-1"), make_artifacts())
    store.put(make_record("run-10"), make_artifacts())

    assert store.delete("run-1") is True

    assert store.get("run-1") is None
    assert catalog.get("run-1") is None
    assert blobs.list_prefix(f"{run_prefix('run-1')}/") == ()
    with pytest.raises(KeyError, match="run not found"):
        store.fetch("run-1")
    sibling = store.fetch("run-10")
    assert dict(sibling.artifacts) == make_artifacts()
    assert blobs.list_prefix(f"{run_prefix('run-10')}/") != ()


def test_delete_missing_run_returns_false() -> None:
    store, _, _ = make_store()
    store.put(make_record(), make_artifacts())

    assert store.delete("missing-run") is False
    assert store.get(RUN_ID) is not None


def test_delete_cleans_partial_states_on_both_sides() -> None:
    orphan_row_store, _, orphan_catalog = make_store()
    orphan_catalog.upsert(make_record("orphan-row"))

    assert orphan_row_store.delete("orphan-row") is True
    assert orphan_row_store.get("orphan-row") is None

    orphan_blob_store, orphan_blobs, _ = make_store()
    orphan_blobs.replace(f"{run_prefix('orphan-blobs')}/actions.parquet", b"payload")

    assert orphan_blob_store.delete("orphan-blobs") is True
    assert orphan_blobs.list_prefix(f"{run_prefix('orphan-blobs')}/") == ()


def test_run_digest_is_deterministic_and_payload_sensitive() -> None:
    first_store, _, _ = make_store()
    second_store, _, _ = make_store()
    first_store.put(make_record(), {"a.parquet": b"one", "b.parquet": b"two"})
    second_store.put(make_record(), {"b.parquet": b"two", "a.parquet": b"one"})
    changed_store, _, _ = make_store()
    changed_store.put(make_record(), {"a.parquet": b"one", "b.parquet": b"changed"})

    left = first_store.get(RUN_ID)
    right = second_store.get(RUN_ID)
    changed = changed_store.get(RUN_ID)
    assert left is not None and right is not None and changed is not None
    assert left.run_digest == right.run_digest
    assert left.run_digest != changed.run_digest


def test_digests_follow_the_repository_hashing_convention() -> None:
    assert artifact_digest(b"payload") == hashlib.sha256(b"payload").hexdigest()

    digests = {
        name: artifact_digest(payload) for name, payload in make_artifacts().items()
    }
    expected = hashlib.sha256(
        b"fca:harness-results:run:v1\0"
        + canonical_configuration_bytes(
            {
                "domain": "harness.results.run.v1",
                "run_id": RUN_ID,
                "artifacts": {name: digests[name] for name in sorted(digests)},
            }
        )
    ).hexdigest()

    assert run_digest(RUN_ID, digests) == expected


def test_put_recomputes_derived_catalog_fields_without_mutating_input() -> None:
    store, _, _ = make_store()
    stale = RunCatalogRecord(
        run_id=RUN_ID,
        status=RunStatus.COMPLETE,
        strategy_id="momentum",
        run_digest="bogus",
        artifact_count=99,
        total_bytes=1,
    )

    stored = store.put(stale, make_artifacts())

    assert stored.run_digest == run_digest(
        RUN_ID,
        {name: artifact_digest(payload) for name, payload in make_artifacts().items()},
    )
    assert stored.artifact_count == 2
    assert stored.total_bytes == len(b"actions-bytes") + len(b"fills-bytes")
    assert stale.run_digest == "bogus"
    assert stale.artifact_count == 99
    assert stale.total_bytes == 1


def test_configuration_digest_survives_the_round_trip() -> None:
    store, _, _ = make_store()
    record = RunCatalogRecord(
        run_id=RUN_ID,
        status=RunStatus.COMPLETE,
        strategy_id="momentum",
        configuration_digest=configuration_hash({"seed": "deterministic"}),
    )
    uploaded = store.put(record, make_artifacts())

    assert uploaded.configuration_digest == record.configuration_digest
    assert store.fetch(RUN_ID).record.configuration_digest == (
        record.configuration_digest
    )


def test_in_memory_adapters_satisfy_the_store_ports() -> None:
    blobs: RunBlobStore = InMemoryRunBlobStore()
    catalog: RunCatalogRepository = InMemoryRunCatalogRepository()

    assert isinstance(blobs, RunBlobStore)
    assert isinstance(catalog, RunCatalogRepository)


def test_store_ports_are_repointed_to_the_lake_ports() -> None:
    blobs: RunBlobStore = InMemoryRunBlobStore()
    catalog: RunCatalogRepository = LakeRunCatalogRepository(InMemoryRunRepository())

    assert isinstance(blobs, ReadableBlobStore)
    assert isinstance(catalog, RunCatalogRepository)


def test_store_round_trips_through_the_lake_run_repository() -> None:
    blobs = InMemoryRunBlobStore()
    store = BacktestResultsStore(
        blobs,
        LakeRunCatalogRepository(InMemoryRunRepository()),
    )

    stored = store.put(make_record(), make_artifacts())

    assert store.get(RUN_ID) == stored
    assert stored.run_digest is not None
    assert stored.artifact_count == 2
    assert stored.metrics["sharpe"] == "1.2345"
    fetched = store.fetch(RUN_ID)
    assert fetched.record == stored
    assert dict(fetched.artifacts) == make_artifacts()
    assert tuple(row.run_id for row in store.list(status=RunStatus.COMPLETE)) == (
        RUN_ID,
    )
    assert store.list(strategy_id="mean-revert") == ()
    assert store.delete("missing-run") is False
    assert store.delete(RUN_ID) is True
    assert store.get(RUN_ID) is None
    assert blobs.list_prefix(f"{run_prefix(RUN_ID)}/") == ()
    assert store.delete(RUN_ID) is False


@pytest.mark.parametrize(
    ("run_id", "match"),
    [
        ("", "non-empty string"),
        ("../escape", "single safe path segment"),
        ("a/b", "single safe path segment"),
        ("a\\b", "single safe path segment"),
        (".", "single safe path segment"),
        ("..", "single safe path segment"),
    ],
)
def test_run_id_must_be_a_safe_path_segment(run_id: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        make_record(run_id)


def test_artifact_names_must_be_safe_relative_paths() -> None:
    store, _, _ = make_store()

    with pytest.raises(ValueError, match="non-empty string"):
        store.put(make_record(), {"": b"payload"})
    for name in (
        "../escape.parquet",
        "/absolute.parquet",
        "a\\b.parquet",
        "dir/../x.parquet",
    ):
        with pytest.raises(ValueError, match="relative POSIX path"):
            store.put(make_record(), {name: b"payload"})


def test_put_rejects_empty_and_non_bytes_uploads() -> None:
    store, _, _ = make_store()

    with pytest.raises(ValueError, match="at least one artifact"):
        store.put(make_record(), {})
    with pytest.raises(TypeError, match="payload must be bytes"):
        store.put(
            make_record(),
            cast("Mapping[str, bytes]", {"actions.parquet": "text"}),
        )


def test_record_validation_rejects_invalid_rows() -> None:
    with pytest.raises(TypeError, match="must be a date, not a datetime"):
        RunCatalogRecord(
            run_id=RUN_ID,
            status=RunStatus.COMPLETE,
            strategy_id="momentum",
            data_start=datetime(2024, 1, 2),
        )
    with pytest.raises(ValueError, match="on or before data_end"):
        RunCatalogRecord(
            run_id=RUN_ID,
            status=RunStatus.COMPLETE,
            strategy_id="momentum",
            data_start=date(2024, 1, 3),
            data_end=date(2024, 1, 2),
        )
    with pytest.raises(ValueError, match="must not be negative"):
        RunCatalogRecord(
            run_id=RUN_ID,
            status=RunStatus.COMPLETE,
            strategy_id="momentum",
            artifact_count=-1,
        )
    with pytest.raises(TypeError, match="string-exact"):
        RunCatalogRecord(
            run_id=RUN_ID,
            status=RunStatus.COMPLETE,
            strategy_id="momentum",
            metrics=cast("Mapping[str, str]", {"sharpe": 1.25}),
        )
    with pytest.raises(ValueError, match="non-empty string"):
        RunCatalogRecord(
            run_id=RUN_ID,
            status=RunStatus.COMPLETE,
            strategy_id="momentum",
            metrics={"sharpe": "  "},
        )
    with pytest.raises(ValueError):
        RunStatus("bogus")
