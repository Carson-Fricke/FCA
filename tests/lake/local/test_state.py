import json
import os
import threading
from datetime import date

import pytest

from lake.catalog.models import CoverageRecord, CoverageStatus
from lake.contracts import AssetClass, DataType, partition_path
from lake.local import (
    LocalCatalogRepository,
    LocalFileIssuer,
    LocalLayout,
    LocalStateError,
)
from lake.local.state import _record_to_json


def _record(symbol: str = "AAPL") -> CoverageRecord:
    object_path = partition_path(
        session_date=date(2024, 1, 19),
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol=symbol,
    )
    return CoverageRecord(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol=symbol,
        session_date=date(2024, 1, 19),
        status=CoverageStatus.COMPLETE,
        object_path=object_path,
        row_count=1,
        byte_size=7,
        checksum="abc123",
    )


def test_layout_uses_environment_and_reset_requires_ownership_marker(tmp_path) -> None:
    configured = tmp_path / "lake-data"
    layout = LocalLayout.from_environment(
        environ={"LAKE_LOCAL_DATA_DIR": str(configured)}
    )

    layout.initialize()

    assert layout.root == configured.resolve()
    assert layout.objects.is_dir()
    assert layout.state.is_dir()
    assert layout.marker.read_text(encoding="ascii") == "market-lake-local-v1\n"
    assert layout.reset() is True
    assert not configured.exists()
    assert layout.reset() is False

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep", encoding="ascii")
    unsafe = LocalLayout(unrelated)
    with pytest.raises(LocalStateError, match="not created by local mode"):
        unsafe.initialize()
    with pytest.raises(LocalStateError, match="without the local-mode marker"):
        unsafe.reset()
    assert (unrelated / "keep.txt").read_text(encoding="ascii") == "keep"


def test_catalog_is_deterministic_persistent_and_filterable(tmp_path) -> None:
    layout = LocalLayout(tmp_path / "local")
    layout.initialize()
    catalog = LocalCatalogRepository(layout.catalog)
    later_symbol = _record("MSFT")
    first_symbol = _record("AAPL")

    catalog.upsert_many((later_symbol, first_symbol))
    first_bytes = layout.catalog.read_bytes()
    catalog.upsert_many((first_symbol, later_symbol))

    assert layout.catalog.read_bytes() == first_bytes
    reloaded = LocalCatalogRepository(layout.catalog)
    assert (
        reloaded.get(
            AssetClass.EQUITY,
            DataType.TRADE,
            "aapl",
            date(2024, 1, 19),
        )
        == first_symbol
    )
    assert reloaded.list(symbol="MSFT") == (later_symbol,)
    assert [record.symbol for record in reloaded.list()] == ["AAPL", "MSFT"]


def test_catalog_rejects_malformed_state_without_overwriting_it(tmp_path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text('{"credentials": "must-not-load"}', encoding="utf-8")
    repository = LocalCatalogRepository(path)

    with pytest.raises(LocalStateError, match="local catalog is invalid"):
        repository.list()

    assert "credentials" in path.read_text(encoding="utf-8")


def test_local_file_issuer_confines_urls_to_existing_objects(tmp_path) -> None:
    root = tmp_path / "objects"
    target = root / "date=2024-01-19" / "AAPL.parquet"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"parquet")
    issuer = LocalFileIssuer(root)

    assert issuer.issue("date=2024-01-19/AAPL.parquet") == target.resolve().as_uri()
    with pytest.raises(ValueError, match="canonical"):
        issuer.issue("../secret")
    with pytest.raises(LocalStateError, match="unavailable"):
        issuer.issue("date=2024-01-19/MISSING.parquet")


def test_concurrent_reader_and_writer_repositories_do_not_corrupt(tmp_path) -> None:
    """An ensure backfill writes through one repository while the API serves
    coverage reads through another; the shared class lock serializes them, so
    a read never overlaps the atomic replace (the Windows killer)."""
    layout = LocalLayout(tmp_path / "local")
    layout.initialize()
    writer = LocalCatalogRepository(layout.catalog)
    reader = LocalCatalogRepository(layout.catalog)
    writer.upsert_many((_record(),))
    failures: list[BaseException] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            try:
                reader.list(symbol="AAPL")
            except BaseException as error:  # noqa: BLE001 - recorded, then re-raised below
                failures.append(error)
                return

    def write() -> None:
        for index in range(50):
            writer.upsert_many((_record("MSFT" if index % 2 else "AAPL"),))

    poller = threading.Thread(target=poll)
    poller.start()
    try:
        write()
    finally:
        stop.set()
        poller.join(timeout=5)
    assert failures == []


def test_catalog_replace_retries_a_transiently_held_target(
    tmp_path, monkeypatch
) -> None:
    """A target handle held briefly by another process (or an antivirus scan)
    denies the first replace; the repository retries instead of failing."""
    layout = LocalLayout(tmp_path / "local")
    layout.initialize()
    repository = LocalCatalogRepository(layout.catalog)
    repository.upsert_many((_record(),))
    real_replace = os.replace
    calls = {"count": 0}
    hold = open(layout.catalog, "rb")  # noqa: SIM115 - released in the finally block

    def flaky_replace(source, target):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 1:
            # First attempt collides with the still-open handle: simulate the
            # reader releasing it right after the denied replace.
            hold.close()
            raise PermissionError(5, "Access is denied")
        return real_replace(source, target)

    monkeypatch.setattr("lake.local.state.os.replace", flaky_replace)
    try:
        repository.upsert_many((_record("MSFT"),))
    finally:
        hold.close()

    assert calls["count"] == 2
    assert repository.list(symbol="MSFT") == (_record("MSFT"),)


def test_catalog_reads_reuse_a_parsed_cache_without_going_stale(tmp_path) -> None:
    """Parsing the whole catalog on every coverage read dominated the API's CPU,
    so the parse is cached per file stamp — and the cache must never serve a
    parse older than the file."""
    layout = LocalLayout(tmp_path / "local")
    layout.initialize()
    writer = LocalCatalogRepository(layout.catalog)
    reader = LocalCatalogRepository(layout.catalog)

    writer.upsert_many((_record("AAPL"),))
    assert reader.list(symbol="AAPL") == (_record("AAPL"),)

    # Every repository instance over the same file shares one coherent cache.
    LocalCatalogRepository(layout.catalog).upsert_many((_record("MSFT"),))
    assert reader.list(symbol="MSFT") == (_record("MSFT"),)

    # A write that bypasses the repository entirely (another process, or the
    # CLI) changes the file stamp, so no stale parse is served.
    payload = json.loads(layout.catalog.read_text(encoding="utf-8"))
    payload["coverage"].append(_record_to_json(_record("NVDA")))
    layout.catalog.write_text(json.dumps(payload), encoding="utf-8")
    assert sorted(record.symbol for record in reader.list()) == [
        "AAPL",
        "MSFT",
        "NVDA",
    ]

    # An upsert mutates its own copy, never the shared cached parse.
    writer.upsert_many((_record("TSLA"),))
    assert len(reader.list()) == 4
