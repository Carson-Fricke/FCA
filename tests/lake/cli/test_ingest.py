from datetime import UTC, date, datetime
from typing import Any, cast

import pytest

from lake.cli.ingest import _select_session, run_ingest
from lake.ingestion.azure import AzureAtomicIngestCommitter


def test_select_session_requires_a_closed_xnys_day() -> None:
    now = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)

    session = _select_session(date(2026, 9, 1), now)

    assert session.session_date == date(2026, 9, 1)
    assert session.close_time <= now


def test_select_session_rejects_future_or_weekend() -> None:
    now = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="closed XNYS"):
        _select_session(date(2026, 9, 3), now)
    with pytest.raises(ValueError, match="closed XNYS"):
        _select_session(date(2026, 9, 5), now)


def test_run_ingest_requires_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LAKE_PROVIDER__API_KEY", raising=False)
    monkeypatch.delenv("LAKE_PROVIDER__API_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="LAKE_PROVIDER__API_KEY"):
        import asyncio

        asyncio.run(run_ingest())


class _Store:
    def __init__(self) -> None:
        self.objects: list[tuple[str, bytes]] = []

    def replace(self, relative_path: str, payload: bytes) -> None:
        self.objects.append((relative_path, payload))


class _Catalog:
    def __init__(self) -> None:
        self.records = []

    def get(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None

    def list(self, **kwargs: Any) -> tuple[Any, ...]:
        del kwargs
        return ()

    def upsert(self, record: Any) -> Any:
        self.records.append(record)
        return record


def test_azure_committer_publishes_objects_before_coverage() -> None:
    from lake.catalog import CoverageRecord, CoverageStatus
    from lake.contracts import AssetClass, DataType
    from lake.ingestion.common import StagedParquetObject
    from lake.storage import ParquetMetadata

    store = _Store()
    catalog = _Catalog()
    committer = AzureAtomicIngestCommitter(
        object_store=cast(Any, store), catalog=cast(Any, catalog)
    )
    metadata = ParquetMetadata("object.parquet", 1, 2, "checksum")
    record = CoverageRecord(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
        session_date=date(2026, 9, 1),
        status=CoverageStatus.COMPLETE,
    )

    committer.commit(
        objects=[StagedParquetObject(metadata, b"ok")],
        coverage=[record],
    )

    assert store.objects == [("object.parquet", b"ok")]
    assert catalog.records == [record]
