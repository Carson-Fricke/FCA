from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from lake.app import create_app
from lake.catalog import CoverageRecord, CoverageStatus, InMemoryCatalogRepository
from lake.models import EnsureRequest
from lake.partition_paths import AssetClass, DataType, partition_path
from lake.query_api import EnsureCallback
from lake.storage import SasIssuer

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
) -> CoverageRecord:
    return CoverageRecord(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol=symbol,
        session_date=session_date,
        status=status,
        object_path=object_path
        if object_path is not None
        else partition_path(
            session_date=session_date,
            asset_class=AssetClass.EQUITY,
            data_type=DataType.TRADE,
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
