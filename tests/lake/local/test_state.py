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
