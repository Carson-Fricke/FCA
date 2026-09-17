import sys
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from lake.catalog.models import CoverageRecord, CoverageStatus
from lake.contracts import AssetClass, DataType
from lake.local import (
    LocalCatalogRepository,
    LocalEnsureLauncher,
    LocalLayout,
    create_local_app,
)
from lake.providers.alpaca.records import NormalizedQuote, NormalizedTrade
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


def test_local_api_file_query_scopes_sas_to_complete_catalog_objects(tmp_path) -> None:
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
            session_date=date(2024, 1, 19),
            status=CoverageStatus.COMPLETE,
            object_path=metadata.path,
            row_count=metadata.rows,
            byte_size=metadata.bytes,
            checksum=metadata.checksum,
        )
    )

    with TestClient(create_local_app(layout=layout, bearer_token="token")) as client:
        unauthenticated = client.post(
            "/v1/query",
            json={
                "mode": "file",
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": "2024-01-19",
                "end_date": "2024-01-19",
            },
        )
        response = client.post(
            "/v1/query",
            json={
                "mode": "file",
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": "2024-01-19",
                "end_date": "2024-01-19",
            },
            headers={"Authorization": "Bearer token"},
        )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.json()["results"] == {
        "requested_sessions": 1,
        "returned_files": 1,
        "complete_sessions": 1,
        "pending_sessions": 0,
        "state": "complete",
    }
    assert response.json()["files"][0]["object_path"] == metadata.path
    assert response.json()["files"][0]["url"].startswith("file://")


def test_local_api_ensure_returns_202_and_authenticated_polling_for_missing_data(
    tmp_path,
) -> None:
    layout = LocalLayout(tmp_path / "local")

    with TestClient(
        create_local_app(
            layout=layout,
            bearer_token="token",
            job_launcher=lambda job: None,
        )
    ) as client:
        response = client.post(
            "/v1/catalog/ensure",
            json={
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": "2024-01-19",
                "end_date": "2024-01-19",
            },
            headers={"Authorization": "Bearer token"},
        )
        polled = client.get(
            response.headers["location"],
            headers={"Authorization": "Bearer token"},
        )
        unauthenticated = client.get(response.headers["location"])

    assert response.status_code == 202
    assert response.headers["location"].startswith("/v1/jobs/ensure-")
    assert response.json()["state"] == "queued"
    assert polled.status_code == 200
    assert polled.json()["state"] == "queued"
    assert unauthenticated.status_code == 401


def test_local_app_installs_the_on_demand_ensure_launcher(tmp_path) -> None:
    layout = LocalLayout(tmp_path / "local")

    app = create_local_app(layout=layout, bearer_token="token")

    assert isinstance(app.state.job_launcher, LocalEnsureLauncher)


class _FakeEquityProvider:
    async def fetch_trades(self, symbols, start, end, *, max_records=None):
        del start, end, max_records
        return (
            NormalizedTrade(
                timestamp=datetime(2024, 1, 19, 14, 31, tzinfo=UTC),
                symbol=symbols,
                trade_id=1,
                exchange="XNAS",
                price=Decimal("190.12"),
                size=10,
                conditions=("@",),
                tape="C",
                update=None,
            ),
        )

    async def fetch_quotes(self, symbols, start, end, *, max_records=None):
        del start, end, max_records
        return (
            NormalizedQuote(
                timestamp=datetime(2024, 1, 19, 14, 31, tzinfo=UTC),
                symbol=symbols,
                bid_exchange="XNAS",
                bid_price=Decimal("190.11"),
                bid_size=10,
                ask_exchange="XNAS",
                ask_price=Decimal("190.13"),
                ask_size=20,
                conditions=(),
                tape="C",
            ),
        )


def test_local_ensure_fetches_a_symbol_it_has_never_ingested(tmp_path) -> None:
    layout = LocalLayout(tmp_path / "local")
    credentials = {
        "LAKE_PROVIDER__API_KEY": "fake-key",
        "LAKE_PROVIDER__API_SECRET": "fake-secret",
    }
    launcher = LocalEnsureLauncher(
        layout,
        environ=credentials,
        equity_provider=_FakeEquityProvider(),
    )

    with TestClient(
        create_local_app(layout=layout, bearer_token="token", job_launcher=launcher)
    ) as client:
        response = client.post(
            "/v1/catalog/ensure",
            json={
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "GLD",
                "start_date": "2024-01-19",
                "end_date": "2024-01-19",
            },
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 200
    assert all(entry["status"] == "complete" for entry in response.json()["entries"])
    records = LocalCatalogRepository(layout.catalog).list(symbol="GLD")
    assert {record.status for record in records} == {CoverageStatus.COMPLETE}
