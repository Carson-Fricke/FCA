from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from lake.app import create_app
from lake.catalog import CoverageRecord, CoverageStatus, InMemoryCatalogRepository
from lake.contracts import AssetClass, DataType
from lake.coverage import CoverageState

TOKEN = "test-secret"


def _record(
    *,
    symbol: str,
    data_type: DataType,
    session_date: date,
    status: CoverageStatus,
    asset_class: AssetClass = AssetClass.EQUITY,
) -> CoverageRecord:
    return CoverageRecord(
        asset_class=asset_class,
        data_type=data_type,
        symbol=symbol,
        session_date=session_date,
        status=status,
        object_path=(
            f"date={session_date}/asset_class={asset_class.value}/"
            f"data_type={data_type.value}/{symbol}.parquet"
        )
        if status is CoverageStatus.COMPLETE
        else None,
        row_count=12 if status is CoverageStatus.COMPLETE else 0,
        byte_size=3456 if status is CoverageStatus.COMPLETE else 0,
        error_message="source unavailable" if status is CoverageStatus.ERROR else None,
    )


def _client(
    repository: InMemoryCatalogRepository,
    *,
    as_of: datetime,
    max_range_days: int = 366,
) -> TestClient:
    return TestClient(
        create_app(
            bearer_token=TOKEN,
            catalog_repository=repository,
            catalog_max_range_days=max_range_days,
            coverage_clock=lambda: as_of,
        )
    )


def _get(client: TestClient, **params: object):
    return client.get(
        "/v1/catalog/coverage",
        params=params,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def test_authenticated_coverage_is_calendar_aware_and_returns_metadata() -> None:
    repository = InMemoryCatalogRepository(
        [
            _record(
                symbol="AAPL",
                data_type=DataType.TRADE,
                session_date=date(2024, 11, 27),
                status=CoverageStatus.COMPLETE,
            ),
            _record(
                symbol="AAPL",
                data_type=DataType.TRADE,
                session_date=date(2024, 11, 29),
                status=CoverageStatus.EMPTY,
            ),
            _record(
                symbol="AAPL",
                data_type=DataType.TRADE,
                session_date=date(2024, 12, 2),
                status=CoverageStatus.ERROR,
            ),
            _record(
                symbol="MSFT",
                data_type=DataType.TRADE,
                session_date=date(2024, 11, 27),
                status=CoverageStatus.COMPLETE,
            ),
            _record(
                symbol="AAPL",
                data_type=DataType.QUOTE,
                session_date=date(2024, 11, 27),
                status=CoverageStatus.COMPLETE,
            ),
        ]
    )

    response = _get(
        _client(repository, as_of=datetime(2024, 12, 3, tzinfo=UTC)),
        asset_class="equity",
        data_type="trade",
        symbol="aapl",
        start_date="2024-11-27",
        end_date="2024-12-02",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["asset_class"] == "equity"
    assert body["data_type"] == "trade"
    assert body["symbol"] == "AAPL"
    assert [entry["session_date"] for entry in body["entries"]] == [
        "2024-11-27",
        "2024-11-29",
        "2024-12-02",
    ]
    assert [entry["status"] for entry in body["entries"]] == [
        CoverageState.COMPLETE,
        CoverageState.EMPTY,
        CoverageState.ERROR,
    ]
    assert body["entries"][0]["row_count"] == 12
    assert body["entries"][0]["object_path"].endswith("/AAPL.parquet")
    assert body["entries"][1]["row_count"] == 0
    assert body["entries"][2]["error_message"] == "source unavailable"


def test_crypto_coverage_marks_the_open_utc_day_partial() -> None:
    repository = InMemoryCatalogRepository(
        [
            _record(
                asset_class=AssetClass.CRYPTO,
                symbol="BTC/USD",
                data_type=DataType.QUOTE,
                session_date=date(2024, 1, 18),
                status=CoverageStatus.COMPLETE,
            ),
            _record(
                asset_class=AssetClass.CRYPTO,
                symbol="BTC/USD",
                data_type=DataType.QUOTE,
                session_date=date(2024, 1, 19),
                status=CoverageStatus.COMPLETE,
            ),
        ]
    )

    response = _get(
        _client(repository, as_of=datetime(2024, 1, 19, 12, tzinfo=UTC)),
        asset_class="crypto",
        data_type="quote",
        symbol="btc/usd",
        start_date="2024-01-18",
        end_date="2024-01-19",
    )

    assert response.status_code == 200
    entries = response.json()["entries"]
    assert [entry["status"] for entry in entries] == ["complete", "partial"]
    assert entries[1]["is_partial"] is True
    assert entries[1]["open_time"] == "2024-01-19T00:00:00Z"
    assert entries[1]["close_time"] == "2024-01-20T00:00:00Z"


def test_coverage_endpoint_requires_authentication() -> None:
    client = TestClient(
        create_app(
            bearer_token=TOKEN,
            catalog_repository=InMemoryCatalogRepository(),
        )
    )

    response = client.get(
        "/v1/catalog/coverage",
        params={
            "asset_class": "equity",
            "data_type": "trade",
            "symbol": "AAPL",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        },
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    ("params", "expected_code"),
    [
        (
            {
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": "2024-01-02",
                "end_date": "2024-01-01",
            },
            "validation_error",
        ),
        (
            {
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": "2024-01-01",
                "end_date": "2024-01-03",
            },
            "range_too_large",
        ),
        (
            {
                "asset_class": "option",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": "2024-01-01",
                "end_date": "2024-01-01",
            },
            "unsupported_asset_class",
        ),
        (
            {
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "../AAPL",
                "start_date": "2024-01-01",
                "end_date": "2024-01-01",
            },
            "validation_error",
        ),
    ],
)
def test_coverage_endpoint_rejects_invalid_or_unbounded_requests(
    params: dict[str, str], expected_code: str
) -> None:
    client = _client(
        InMemoryCatalogRepository(),
        as_of=datetime(2024, 1, 5, tzinfo=UTC),
        max_range_days=2,
    )

    response = _get(client, **params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code


def test_coverage_endpoint_rejects_unknown_query_fields() -> None:
    client = _client(
        InMemoryCatalogRepository(),
        as_of=datetime(2024, 1, 5, tzinfo=UTC),
    )

    response = _get(
        client,
        asset_class="equity",
        data_type="trade",
        symbol="AAPL",
        start_date="2024-01-01",
        end_date="2024-01-01",
        unexpected="value",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
