from __future__ import annotations

from datetime import date

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from lake.app import create_app
from lake.catalog import CoverageRecord, CoverageStatus, InMemoryCatalogRepository
from lake.contracts import AssetClass, DataType, partition_path
from lake.http.auth import BearerAuthenticator
from lake.http.routes.raw import create_raw_router

TOKEN = "test-secret"
SESSION_DATE = date(2024, 1, 18)


class RecordingSasIssuer:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def issue(self, object_path: str) -> str:
        self.paths.append(object_path)
        return f"https://blob.test/{object_path}?read-only-expiring"


def _path(
    *,
    session_date: date = SESSION_DATE,
    asset_class: AssetClass = AssetClass.EQUITY,
    data_type: DataType = DataType.TRADE,
    symbol: str = "AAPL",
) -> str:
    return partition_path(
        session_date=session_date,
        asset_class=asset_class,
        data_type=data_type,
        symbol=symbol,
    )


def _record(*, object_path: str | None = None) -> CoverageRecord:
    return CoverageRecord(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
        session_date=SESSION_DATE,
        status=CoverageStatus.COMPLETE,
        object_path=_path() if object_path is None else object_path,
        row_count=12,
        byte_size=3456,
        checksum="checksum",
    )


def _client(
    catalog: InMemoryCatalogRepository,
    issuer: RecordingSasIssuer | None,
) -> TestClient:
    application = create_app(
        bearer_token=TOKEN,
        catalog_repository=catalog,
        sas_issuer=issuer,
    )
    application.include_router(
        create_raw_router(),
        dependencies=[Depends(BearerAuthenticator(TOKEN))],
    )
    return TestClient(application)


def _get(client: TestClient, object_path: str, *, authenticated: bool = True):
    headers = {"Authorization": f"Bearer {TOKEN}"} if authenticated else None
    return client.get(
        "/v1/raw",
        params={"object_path": object_path},
        headers=headers,
    )


def test_authenticated_raw_object_returns_known_catalog_and_sas_metadata() -> None:
    object_path = _path(
        session_date=date(2024, 1, 21),
        asset_class=AssetClass.CRYPTO,
        data_type=DataType.QUOTE,
        symbol="BTC/USD",
    )
    record = CoverageRecord(
        asset_class=AssetClass.CRYPTO,
        data_type=DataType.QUOTE,
        symbol="BTC/USD",
        session_date=date(2024, 1, 21),
        status=CoverageStatus.COMPLETE,
        object_path=object_path,
        row_count=24,
        byte_size=6789,
        checksum="crypto-checksum",
    )
    issuer = RecordingSasIssuer()

    response = _get(
        _client(InMemoryCatalogRepository([record]), issuer),
        object_path,
    )

    url = f"https://blob.test/{object_path}?read-only-expiring"
    assert response.status_code == 200
    assert response.json() == {
        "asset_class": "crypto",
        "data_type": "quote",
        "symbol": "BTC/USD",
        "session_date": "2024-01-21",
        "status": "complete",
        "object_path": object_path,
        "url": url,
        "sas_url": url,
        "row_count": 24,
        "byte_size": 6789,
        "checksum": "crypto-checksum",
    }
    assert issuer.paths == [object_path]


def test_raw_object_requires_authentication_before_sas_issuance() -> None:
    issuer = RecordingSasIssuer()
    client = _client(InMemoryCatalogRepository([_record()]), issuer)

    response = _get(client, _path(), authenticated=False)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"
    assert issuer.paths == []


def test_unknown_catalog_path_does_not_receive_sas_credentials() -> None:
    issuer = RecordingSasIssuer()

    response = _get(
        _client(InMemoryCatalogRepository([_record()]), issuer),
        _path(symbol="MSFT"),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "raw_object_not_found"
    assert issuer.paths == []


@pytest.mark.parametrize(
    "object_path",
    [
        "../outside.parquet",
        "/date=2024-01-18/asset_class=equity/data_type=trade/AAPL.parquet",
        r"date=2024-01-18\asset_class=equity\data_type=trade\AAPL.parquet",
        "date=2024-01-18/asset_class=equity/data_type=trade/../AAPL.parquet",
        "date=2024-02-30/asset_class=equity/data_type=trade/AAPL.parquet",
        "date=2024-01-18/asset_class=futures/data_type=trade/AAPL.parquet",
        "date=2024-01-18/asset_class=equity/data_type=bars/AAPL.parquet",
        "date=2024-01-18/asset_class=equity/data_type=trade/aapl.parquet",
        "date=2024-01-18/asset_class=equity/data_type=trade/%2E%2E.parquet",
        "date=2024-01-18/asset_class=equity/data_type=trade/A%2F..%2FB.parquet",
    ],
)
def test_malformed_or_out_of_scope_paths_are_rejected_before_sas(
    object_path: str,
) -> None:
    issuer = RecordingSasIssuer()

    response = _get(
        _client(InMemoryCatalogRepository([_record()]), issuer),
        object_path,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_object_path"
    assert issuer.paths == []


@pytest.mark.parametrize(
    "catalog_path",
    [
        _path(session_date=date(2024, 1, 19)),
        _path(asset_class=AssetClass.OPTION),
        _path(data_type=DataType.QUOTE),
        _path(symbol="MSFT"),
        "../outside.parquet",
    ],
)
def test_catalog_key_and_object_path_must_match_before_sas_issuance(
    catalog_path: str,
) -> None:
    issuer = RecordingSasIssuer()

    response = _get(
        _client(InMemoryCatalogRepository([_record(object_path=catalog_path)]), issuer),
        _path(),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "raw_object_not_found"
    assert issuer.paths == []


def test_raw_object_reports_missing_sas_configuration_without_ensuring() -> None:
    def ensure(_: object) -> None:
        pytest.fail("raw object lookup must not trigger ensure")

    client = _client(InMemoryCatalogRepository([_record()]), None)
    client.app.state.ensure_callback = ensure

    response = _get(
        client,
        _path(),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "sas_unavailable"
