from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from lake.app import create_app
from lake.catalog import (
    ContractRecord,
    CoverageRecord,
    CoverageStatus,
    InMemoryCatalogRepository,
    InMemoryContractRepository,
    OptionType,
)
from lake.catalog.front_sets import DailyFrontSet, InMemoryDailyFrontSetRepository
from lake.contracts import AssetClass, DataType, partition_path
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
            partition_path(
                session_date=session_date,
                asset_class=asset_class,
                data_type=data_type,
                symbol=symbol,
            )
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
    contracts: InMemoryContractRepository | None = None,
    front_sets: InMemoryDailyFrontSetRepository | None = None,
) -> TestClient:
    client = TestClient(
        create_app(
            bearer_token=TOKEN,
            catalog_repository=repository,
            catalog_max_range_days=max_range_days,
            coverage_clock=lambda: as_of,
        )
    )
    if contracts is not None:
        client.app.state.contract_repository = contracts
    if front_sets is not None:
        client.app.state.front_set_repository = front_sets
    return client


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


def test_coverage_filters_catalog_rows_by_the_complete_requested_partition() -> None:
    requested_date = date(2024, 11, 27)
    repository = InMemoryCatalogRepository(
        [
            _record(
                symbol="AAPL",
                data_type=DataType.TRADE,
                session_date=requested_date,
                status=CoverageStatus.COMPLETE,
            ),
            _record(
                symbol="AAPL",
                data_type=DataType.QUOTE,
                session_date=requested_date,
                status=CoverageStatus.COMPLETE,
            ),
            _record(
                symbol="MSFT",
                data_type=DataType.TRADE,
                session_date=requested_date,
                status=CoverageStatus.COMPLETE,
            ),
        ]
    )

    response = _get(
        _client(repository, as_of=datetime(2024, 12, 3, tzinfo=UTC)),
        asset_class="equity",
        data_type="trade",
        symbol="aapl",
        start_date=requested_date.isoformat(),
        end_date=requested_date.isoformat(),
    )

    assert response.status_code == 200
    assert response.json()["entries"] == [
        {
            "session_date": requested_date.isoformat(),
            "open_time": "2024-11-27T14:30:00Z",
            "close_time": "2024-11-27T21:00:00Z",
            "is_partial": False,
            "status": "complete",
            "object_path": "date=2024-11-27/asset_class=equity/data_type=trade/AAPL.parquet",
            "row_count": 12,
            "byte_size": 3456,
            "checksum": None,
            "error_message": None,
        }
    ]


def test_crypto_coverage_includes_every_utc_day_and_marks_the_open_day_partial() -> (
    None
):
    session_dates = tuple(date(2024, 1, day) for day in range(18, 23))
    repository = InMemoryCatalogRepository(
        [
            _record(
                asset_class=AssetClass.CRYPTO,
                symbol="BTC/USD",
                data_type=DataType.QUOTE,
                session_date=session_date,
                status=CoverageStatus.COMPLETE,
            )
            for session_date in session_dates
        ]
    )

    response = _get(
        _client(repository, as_of=datetime(2024, 1, 22, 12, tzinfo=UTC)),
        asset_class="crypto",
        data_type="quote",
        symbol="btc/usd",
        start_date="2024-01-18",
        end_date="2024-01-22",
    )

    assert response.status_code == 200
    body = response.json()
    entries = body["entries"]
    assert body["symbol"] == "BTC/USD"
    assert [entry["session_date"] for entry in entries] == [
        session_date.isoformat() for session_date in session_dates
    ]
    assert [entry["status"] for entry in entries] == [
        "complete",
        "complete",
        "complete",
        "complete",
        "partial",
    ]
    assert entries[2]["object_path"] == (
        "date=2024-01-20/asset_class=crypto/data_type=quote/BTC%2FUSD.parquet"
    )
    assert entries[-1]["is_partial"] is True
    assert entries[-1]["open_time"] == "2024-01-22T00:00:00Z"
    assert entries[-1]["close_time"] == "2024-01-23T00:00:00Z"


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


def test_option_coverage_returns_selected_contracts_and_osi_partition_metadata() -> (
    None
):
    contract = ContractRecord(
        symbol="SPY240119C00500000",
        underlying="SPY",
        expiration_date=date(2024, 1, 19),
        strike_price=Decimal("500"),
        option_type=OptionType.CALL,
    )
    repository = InMemoryCatalogRepository(
        [
            _record(
                symbol=contract.symbol,
                data_type=DataType.TRADE,
                session_date=date(2024, 1, 19),
                status=CoverageStatus.COMPLETE,
                asset_class=AssetClass.OPTION,
            )
        ]
    )

    response = _get(
        _client(
            repository,
            as_of=datetime(2024, 1, 22, tzinfo=UTC),
            contracts=InMemoryContractRepository([contract]),
        ),
        asset_class="option",
        data_type="trade",
        underlying="spy",
        contract_symbols=contract.symbol,
        start_date="2024-01-19",
        end_date="2024-01-19",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["underlying"] == "SPY"
    assert body["contracts"] == [contract.symbol]
    assert body["entries"][0]["contract_symbol"] == contract.symbol
    assert body["entries"][0]["status"] == "complete"


def test_option_underlying_coverage_uses_the_persisted_daily_front_set() -> None:
    contract = ContractRecord(
        symbol="SPY240119P00450000",
        underlying="SPY",
        expiration_date=date(2024, 1, 19),
        strike_price=Decimal("450"),
        option_type=OptionType.PUT,
    )
    response = _get(
        _client(
            InMemoryCatalogRepository(),
            as_of=datetime(2024, 1, 22, tzinfo=UTC),
            contracts=InMemoryContractRepository([contract]),
            front_sets=InMemoryDailyFrontSetRepository(
                [DailyFrontSet("SPY", date(2024, 1, 19), (contract,))]
            ),
        ),
        asset_class="option",
        data_type="quote",
        underlying="spy",
        start_date="2024-01-19",
        end_date="2024-01-19",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["contracts"] == [contract.symbol]
    assert body["entries"][0]["contract_symbol"] == contract.symbol
    assert body["entries"][0]["status"] == "missing"


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
