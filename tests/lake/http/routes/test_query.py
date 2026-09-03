from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

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
from lake.contracts import AssetClass, DataType, partition_path
from lake.http.models import EnsureRequest
from lake.http.routes.query import EnsureCallback
from lake.storage import LocalFilesystemStore, ParquetWriter, SasIssuer

TOKEN = "test-secret"
AS_OF = datetime(2024, 1, 22, 12, tzinfo=UTC)


class RecordingSasIssuer:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def issue(self, object_path: str) -> str:
        self.paths.append(object_path)
        return f"https://blob.test/{object_path}?read-only-expiring"


def _record(
    *,
    session_date: date,
    status: CoverageStatus = CoverageStatus.COMPLETE,
    object_path: str | None = None,
    symbol: str = "AAPL",
    asset_class: AssetClass = AssetClass.EQUITY,
    data_type: DataType = DataType.TRADE,
) -> CoverageRecord:
    return CoverageRecord(
        asset_class=asset_class,
        data_type=data_type,
        symbol=symbol,
        session_date=session_date,
        status=status,
        object_path=object_path
        if object_path is not None
        else partition_path(
            session_date=session_date,
            asset_class=asset_class,
            data_type=data_type,
            symbol=symbol,
        )
        if status is CoverageStatus.COMPLETE
        else None,
        row_count=12 if status is CoverageStatus.COMPLETE else 0,
        byte_size=3456 if status is CoverageStatus.COMPLETE else 0,
        checksum="checksum" if status is CoverageStatus.COMPLETE else None,
    )


def _client(
    catalog: InMemoryCatalogRepository,
    issuer: SasIssuer | None = None,
    *,
    ensure_callback: EnsureCallback | None = None,
    max_range_days: int = 366,
) -> TestClient:
    return TestClient(
        create_app(
            bearer_token=TOKEN,
            catalog_repository=catalog,
            catalog_max_range_days=max_range_days,
            coverage_clock=lambda: AS_OF,
            sas_issuer=issuer,
            ensure_callback=ensure_callback,
        )
    )


def _request(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "mode": "file",
        "asset_class": "equity",
        "data_type": "trade",
        "symbol": "aapl",
        "start_date": "2024-01-18",
        "end_date": "2024-01-19",
        "ensure": False,
    }
    body.update(overrides)
    return body


def _option_contract() -> ContractRecord:
    return ContractRecord(
        symbol="SPY240119C00500000",
        underlying="SPY",
        expiration_date=date(2024, 1, 19),
        strike_price=Decimal("500"),
        option_type=OptionType.CALL,
    )


def _option_record(
    contract: ContractRecord,
    *,
    data_type: DataType = DataType.TRADE,
    byte_size: int = 3456,
) -> CoverageRecord:
    return CoverageRecord(
        asset_class=AssetClass.OPTION,
        data_type=data_type,
        symbol=contract.symbol,
        session_date=date(2024, 1, 19),
        status=CoverageStatus.COMPLETE,
        object_path=partition_path(
            session_date=date(2024, 1, 19),
            asset_class=AssetClass.OPTION,
            data_type=data_type,
            symbol=contract.symbol,
        ),
        row_count=1,
        byte_size=byte_size,
        checksum="option-checksum",
    )


def _post(client: TestClient, body: dict[str, object]):
    return client.post(
        "/v1/query",
        json=body,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def test_cached_file_query_returns_only_complete_files_and_catalog_metadata() -> None:
    complete_path = partition_path(
        session_date=date(2024, 1, 18),
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
    )
    issuer = RecordingSasIssuer()
    catalog = InMemoryCatalogRepository(
        [
            _record(session_date=date(2024, 1, 18)),
            _record(session_date=date(2024, 1, 19), status=CoverageStatus.EMPTY),
        ]
    )

    response = _post(_client(catalog, issuer), _request())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "coverage_incomplete"
    assert issuer.paths == []

    complete_only = _post(
        _client(
            InMemoryCatalogRepository([_record(session_date=date(2024, 1, 18))]),
            issuer,
        ),
        _request(start_date="2024-01-18", end_date="2024-01-18"),
    )

    assert complete_only.status_code == 200
    body = complete_only.json()
    assert set(body) == {"files", "results", "limitations"}
    assert body["results"] == {
        "requested_sessions": 1,
        "returned_files": 1,
        "complete_sessions": 1,
        "pending_sessions": 0,
        "state": "complete",
    }
    assert body["files"] == [
        {
            "asset_class": "equity",
            "data_type": "trade",
            "symbol": "AAPL",
            "session_date": "2024-01-18",
            "object_path": complete_path,
            "url": f"https://blob.test/{complete_path}?read-only-expiring",
            "sas_url": f"https://blob.test/{complete_path}?read-only-expiring",
            "row_count": 12,
            "byte_size": 3456,
            "checksum": "checksum",
        }
    ]
    assert issuer.paths == [complete_path]


def test_option_file_query_returns_selected_osi_file_with_authentication() -> None:
    contract = _option_contract()
    issuer = RecordingSasIssuer()
    catalog = InMemoryCatalogRepository([_option_record(contract)])
    client = _client(catalog, issuer)
    client.app.state.contract_repository = InMemoryContractRepository([contract])

    response = _post(
        client,
        _request(
            asset_class="option",
            data_type="trade",
            symbol=None,
            underlying="SPY",
            contract_symbols=[contract.symbol],
            start_date="2024-01-19",
            end_date="2024-01-19",
        ),
    )

    assert response.status_code == 200
    assert response.json()["files"][0]["asset_class"] == "option"
    assert response.json()["files"][0]["symbol"] == contract.symbol
    assert issuer.paths == [_option_record(contract).object_path]


def test_crypto_file_query_returns_canonical_files_for_every_weekend_utc_day() -> None:
    session_dates = tuple(date(2024, 1, day) for day in range(19, 22))
    catalog = InMemoryCatalogRepository(
        [
            _record(
                asset_class=AssetClass.CRYPTO,
                data_type=DataType.QUOTE,
                symbol="BTC/USD",
                session_date=session_date,
            )
            for session_date in session_dates
        ]
    )
    issuer = RecordingSasIssuer()

    response = _post(
        _client(catalog, issuer),
        _request(
            asset_class="crypto",
            data_type="quote",
            symbol="btc/usd",
            start_date="2024-01-19",
            end_date="2024-01-21",
        ),
    )

    expected_paths = [
        partition_path(
            session_date=session_date,
            asset_class=AssetClass.CRYPTO,
            data_type=DataType.QUOTE,
            symbol="BTC/USD",
        )
        for session_date in session_dates
    ]
    assert response.status_code == 200
    assert response.json()["results"] == {
        "requested_sessions": 3,
        "returned_files": 3,
        "complete_sessions": 3,
        "pending_sessions": 0,
        "state": "complete",
    }
    assert [item["object_path"] for item in response.json()["files"]] == expected_paths
    assert all(path.endswith("/BTC%2FUSD.parquet") for path in issuer.paths)
    assert issuer.paths == expected_paths


def test_crypto_file_query_treats_the_open_utc_day_as_partial() -> None:
    issuer = RecordingSasIssuer()
    catalog = InMemoryCatalogRepository(
        [
            _record(
                asset_class=AssetClass.CRYPTO,
                symbol="BTC/USD",
                session_date=AS_OF.date(),
            )
        ]
    )

    response = _post(
        _client(catalog, issuer),
        _request(
            asset_class="crypto",
            symbol="BTC/USD",
            start_date="2024-01-22",
            end_date="2024-01-22",
        ),
    )

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "coverage_incomplete",
        "message": (
            "requested coverage is incomplete; set ensure=true to request coverage"
        ),
    }
    assert issuer.paths == []


def test_file_query_requires_authentication_and_rejects_sql_fields() -> None:
    client = _client(InMemoryCatalogRepository(), RecordingSasIssuer())

    unauthenticated = client.post("/v1/query", json=_request())
    sql_request = _post(client, _request(sql="select * from data"))
    missing_mode_body = _request()
    del missing_mode_body["mode"]
    missing_mode = _post(client, missing_mode_body)

    assert unauthenticated.status_code == 401
    assert sql_request.status_code == 422
    assert sql_request.json()["error"]["code"] == "validation_error"
    assert missing_mode.status_code == 422
    assert missing_mode.json()["error"]["code"] == "validation_error"


def test_incomplete_file_query_can_invoke_injected_ensure_and_then_issue_sas() -> None:
    catalog = InMemoryCatalogRepository()
    issuer = RecordingSasIssuer()
    ensured: list[EnsureRequest] = []

    def ensure(request: EnsureRequest) -> None:
        ensured.append(request)
        catalog.upsert(_record(session_date=date(2024, 1, 18)))
        catalog.upsert(_record(session_date=date(2024, 1, 19)))

    response = _post(
        _client(catalog, issuer, ensure_callback=ensure),
        _request(ensure=True),
    )

    assert response.status_code == 200
    assert len(ensured) == 1
    assert ensured[0].symbol == "aapl"
    assert response.json()["results"]["state"] == "complete"
    assert len(response.json()["files"]) == 2
    assert len(issuer.paths) == 2


def test_requested_ensure_requires_an_injected_callback() -> None:
    issuer = RecordingSasIssuer()
    catalog = InMemoryCatalogRepository()

    response = _post(
        _client(catalog, issuer),
        _request(ensure=True),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ensure_unavailable"
    assert issuer.paths == []


def test_requested_ensure_returns_pending_complete_files_without_sas_for_gaps() -> None:
    issuer = RecordingSasIssuer()
    catalog = InMemoryCatalogRepository([_record(session_date=date(2024, 1, 18))])
    callback_calls: list[EnsureRequest] = []

    def ensure(request: EnsureRequest) -> None:
        callback_calls.append(request)

    response = _post(
        _client(catalog, issuer, ensure_callback=ensure),
        _request(ensure=True),
    )

    assert response.status_code == 202
    assert response.json()["results"] == {
        "requested_sessions": 2,
        "returned_files": 1,
        "complete_sessions": 1,
        "pending_sessions": 1,
        "state": "ensure_pending",
    }
    assert len(response.json()["files"]) == 1
    assert len(callback_calls) == 1
    assert len(issuer.paths) == 1


def test_file_query_rejects_bounds_and_malformed_complete_catalog_paths() -> None:
    issuer = RecordingSasIssuer()
    catalog = InMemoryCatalogRepository(
        [
            _record(
                session_date=date(2024, 1, 18),
                object_path="../outside.parquet",
            )
        ]
    )
    client = _client(catalog, issuer, max_range_days=1)

    too_large = _post(client, _request())
    malformed = _post(
        _client(catalog, issuer),
        _request(start_date="2024-01-18", end_date="2024-01-18"),
    )

    assert too_large.status_code == 422
    assert too_large.json()["error"]["code"] == "range_too_large"
    assert malformed.status_code == 409
    assert malformed.json()["error"]["code"] == "coverage_incomplete"
    assert issuer.paths == []


def test_option_sql_query_uses_only_selected_local_parquet(tmp_path: Path) -> None:
    contract = _option_contract()
    ParquetWriter(LocalFilesystemStore(tmp_path)).write(
        [
            {
                "timestamp": "2024-01-19T14:31:00Z",
                "symbol": contract.symbol,
                "exchange": "XOPR",
                "price": "5.25",
                "size": 2,
                "condition": "@",
            }
        ],
        session_date=date(2024, 1, 19),
        asset_class=AssetClass.OPTION,
        data_type=DataType.TRADE,
        symbol=contract.symbol,
    )
    catalog = InMemoryCatalogRepository([_option_record(contract)])
    client = TestClient(
        create_app(
            bearer_token=TOKEN,
            catalog_repository=catalog,
            coverage_clock=lambda: AS_OF,
            query_file_root=tmp_path,
        )
    )
    client.app.state.contract_repository = InMemoryContractRepository([contract])

    response = _post(
        client,
        _request(
            mode="sql",
            asset_class="option",
            data_type="trade",
            symbol=None,
            underlying="SPY",
            contract_symbols=[contract.symbol],
            start_date="2024-01-19",
            end_date="2024-01-19",
            sql="SELECT symbol, price FROM data",
        ),
    )

    assert response.status_code == 200
    assert response.json()["rows"] == [
        {"symbol": contract.symbol, "price": "5.250000000000000000"}
    ]
    assert response.json()["forced"] is False


def test_crypto_sql_query_reads_a_canonical_sunday_partition(tmp_path: Path) -> None:
    session_date = date(2024, 1, 21)
    metadata = ParquetWriter(LocalFilesystemStore(tmp_path)).write(
        [
            {
                "timestamp": "2024-01-21T12:30:00Z",
                "symbol": "BTC/USD",
                "trade_id": 1,
                "exchange": "CBSE",
                "price": "41234.125",
                "size": "0.01",
                "taker_side": "B",
            }
        ],
        session_date=session_date,
        asset_class=AssetClass.CRYPTO,
        data_type=DataType.TRADE,
        symbol="BTC/USD",
    )
    catalog = InMemoryCatalogRepository(
        [
            _record(
                asset_class=AssetClass.CRYPTO,
                symbol="BTC/USD",
                session_date=session_date,
                object_path=metadata.path,
            )
        ]
    )
    client = TestClient(
        create_app(
            bearer_token=TOKEN,
            catalog_repository=catalog,
            coverage_clock=lambda: AS_OF,
            query_file_root=tmp_path,
        )
    )

    response = _post(
        client,
        _request(
            mode="sql",
            asset_class="crypto",
            symbol="btc/usd",
            start_date="2024-01-21",
            end_date="2024-01-21",
            sql="SELECT symbol, trade_id FROM data",
        ),
    )

    assert metadata.path == (
        "date=2024-01-21/asset_class=crypto/data_type=trade/BTC%2FUSD.parquet"
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [{"symbol": "BTC/USD", "trade_id": 1}]
    assert response.json()["forced"] is False
