import sys
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from lake.catalog.models import CoverageRecord, CoverageStatus
from lake.contracts import AssetClass, DataType
from lake.local import LocalCatalogRepository, LocalLayout, create_local_app
from lake.storage.parquet import LocalFilesystemStore, ParquetWriter


def test_local_app_starts_authenticated_over_local_adapters_without_azure(
    tmp_path,
) -> None:
    azure_modules_before = {name for name in sys.modules if name.startswith("azure")}
    layout = LocalLayout(tmp_path / "local")

    with TestClient(
        create_local_app(layout=layout, bearer_token="local-token")
    ) as client:
        health = client.get("/health")
        unauthenticated = client.get("/v1/auth/check")
        authenticated = client.get(
            "/v1/auth/check",
            headers={"Authorization": "Bearer local-token"},
        )
        coverage = client.get(
            "/v1/catalog/coverage",
            params={
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": "2024-01-19",
                "end_date": "2024-01-19",
            },
            headers={"Authorization": "Bearer local-token"},
        )

    assert health.json() == {"status": "ok"}
    assert unauthenticated.status_code == 401
    assert authenticated.json() == {"status": "ok"}
    assert coverage.status_code == 200
    assert coverage.json()["entries"][0]["status"] == "missing"
    assert layout.marker.is_file()
    assert {name for name in sys.modules if name.startswith("azure")} == (
        azure_modules_before
    )


def test_local_app_uses_persistent_catalog_and_bounded_query(tmp_path) -> None:
    layout = LocalLayout(tmp_path / "local")
    layout.initialize()
    metadata = ParquetWriter(LocalFilesystemStore(layout.objects)).write(
        (
            {
                "timestamp": datetime(2024, 1, 19, 14, 31, tzinfo=UTC),
                "symbol": "AAPL",
                "trade_id": 7,
                "exchange": "XNAS",
                "price": Decimal("190.12"),
                "size": 10,
                "conditions": ("@",),
                "tape": "C",
                "update": None,
            },
        ),
        session_date=datetime(2024, 1, 19, tzinfo=UTC).date(),
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
    )
    LocalCatalogRepository(layout.catalog).upsert(
        CoverageRecord(
            asset_class=AssetClass.EQUITY,
            data_type=DataType.TRADE,
            symbol="AAPL",
            session_date=datetime(2024, 1, 19, tzinfo=UTC).date(),
            status=CoverageStatus.COMPLETE,
            object_path=metadata.path,
            row_count=metadata.rows,
            byte_size=metadata.bytes,
            checksum=metadata.checksum,
        )
    )

    with TestClient(create_local_app(layout=layout, bearer_token="token")) as client:
        response = client.get(
            "/v1/catalog/coverage",
            params={
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": "2024-01-19",
                "end_date": "2024-01-19",
            },
            headers={"Authorization": "Bearer token"},
        )
        query = client.post(
            "/v1/query",
            json={
                "mode": "sql",
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": "2024-01-19",
                "end_date": "2024-01-19",
                "sql": "select symbol, trade_id from data",
            },
            headers={"Authorization": "Bearer token"},
        )

    assert response.json()["entries"][0]["status"] == "complete"
    assert query.status_code == 200
    assert query.json()["rows"] == [{"symbol": "AAPL", "trade_id": 7}]
