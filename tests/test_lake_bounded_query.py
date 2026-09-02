from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient

from lake.app import create_app
from lake.bounded_query import (
    BoundedDuckDBQuery,
    EnsuredQueryFile,
    ForceOverrideNotAuthorizedError,
    QueryLimits,
    ResultLimitExceededError,
    StoragePathNotAllowedError,
)
from lake.catalog import CoverageRecord, CoverageStatus, InMemoryCatalogRepository
from lake.parquet import LocalFilesystemStore, ParquetWriter
from lake.partition_paths import AssetClass, DataType, partition_path

TOKEN = "test-secret"
AS_OF = datetime(2024, 1, 22, 12, tzinfo=UTC)


def _write_fixture(root: Path, session_date: date = date(2024, 1, 18)) -> str:
    path = partition_path(
        session_date=session_date,
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
    )
    ParquetWriter(LocalFilesystemStore(root)).write(
        [
            {
                "timestamp": "2024-01-18T14:30:00Z",
                "symbol": "AAPL",
                "trade_id": 1,
                "exchange": "XNAS",
                "price": "190.125",
                "size": 100,
                "conditions": ["@"],
                "tape": "C",
                "update": None,
            },
            {
                "timestamp": "2024-01-18T14:31:00Z",
                "symbol": "AAPL",
                "trade_id": 2,
                "exchange": "XNAS",
                "price": "191.125",
                "size": 200,
                "conditions": ["@"],
                "tape": "C",
                "update": None,
            },
        ],
        session_date=session_date,
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
    )
    return path


def _catalog(
    root: Path, path: str, byte_size: int | None = None
) -> InMemoryCatalogRepository:
    size = (
        (root / Path(*path.split("/"))).stat().st_size
        if byte_size is None
        else byte_size
    )
    return InMemoryCatalogRepository(
        [
            CoverageRecord(
                asset_class=AssetClass.EQUITY,
                data_type=DataType.TRADE,
                symbol="AAPL",
                session_date=date(2024, 1, 18),
                status=CoverageStatus.COMPLETE,
                object_path=path,
                row_count=2,
                byte_size=size,
            )
        ]
    )


def _client(
    root: Path,
    *,
    byte_size: int | None = None,
    max_scan_bytes: int = 1_073_741_824,
    max_result_bytes: int = 33_554_432,
    authorized_force_override: bool = False,
) -> TestClient:
    path = partition_path(
        session_date=date(2024, 1, 18),
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
    )
    return TestClient(
        create_app(
            bearer_token=TOKEN,
            catalog_repository=_catalog(root, path, byte_size),
            coverage_clock=lambda: AS_OF,
            query_file_root=root,
            sql_max_scan_bytes=max_scan_bytes,
            sql_max_result_bytes=max_result_bytes,
            authorized_force_override=authorized_force_override,
        )
    )


def _post(client: TestClient, **overrides: object):
    body: dict[str, object] = {
        "mode": "sql",
        "asset_class": "equity",
        "data_type": "trade",
        "symbol": "AAPL",
        "start_date": "2024-01-18",
        "end_date": "2024-01-18",
        "sql": "SELECT trade_id, price FROM data ORDER BY trade_id",
    }
    body.update(overrides)
    return client.post(
        "/v1/query",
        json=body,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def test_sql_query_reads_only_ensured_fixture_and_returns_json_metadata(
    tmp_path: Path,
) -> None:
    _write_fixture(tmp_path)

    response = _post(_client(tmp_path))

    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "json"
    assert body["columns"] == ["trade_id", "price"]
    assert [row["trade_id"] for row in body["rows"]] == [1, 2]
    assert body["estimated_scan_bytes"] > 0
    assert body["result_bytes"] > 0
    assert body["forced"] is False
    assert "canonical complete request partitions" in body["limitations"][0]


def test_sql_query_can_return_arrow_ipc_stream(tmp_path: Path) -> None:
    _write_fixture(tmp_path)

    response = _post(
        _client(tmp_path),
        output="arrow",
        sql="SELECT trade_id, price FROM data ORDER BY trade_id",
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.apache.arrow.stream"
    reader = ipc.open_stream(response.content)
    table = reader.read_all()
    assert table.column_names == ["trade_id", "price"]
    assert table.column("trade_id").to_pylist() == [1, 2]
    assert int(response.headers["x-query-estimated-scan-bytes"]) > 0


def test_sql_requires_complete_coverage_and_rejects_unknown_request_fields(
    tmp_path: Path,
) -> None:
    _write_fixture(tmp_path)
    client = _client(tmp_path)

    incomplete = _post(client, start_date="2024-01-17")
    unknown = _post(client, storage_path="C:/outside.parquet")

    assert incomplete.status_code == 409
    assert incomplete.json()["error"]["code"] == "coverage_incomplete"
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_parquet('C:/outside.parquet')",
        "SELECT parquet_schema('C:/outside.parquet') FROM data",
        "SELECT * FROM '/absolute/outside.parquet'",
        "DROP TABLE data",
        "SELECT * FROM data; SELECT 1",
        "SELECT * FROM data WHERE 1 = (SELECT 1); INSTALL httpfs",
        "SELECT * FROM data CROSS JOIN range(1000000000)",
        "SELECT * FROM data, unnest([1,2])",
    ],
)
def test_sql_rejects_storage_paths_dangerous_statements_and_multiple_statements(
    tmp_path: Path, sql: str
) -> None:
    _write_fixture(tmp_path)

    response = _post(_client(tmp_path), sql=sql)

    assert response.status_code == 422
    assert response.json()["error"]["code"] in {
        "sql_not_allowed",
        "storage_path_not_allowed",
    }


def test_scan_cap_is_estimated_from_catalog_metadata_and_force_is_fail_closed(
    tmp_path: Path,
) -> None:
    _write_fixture(tmp_path)
    client = _client(
        tmp_path,
        byte_size=100,
        max_scan_bytes=99,
        max_result_bytes=99,
    )

    blocked = _post(client)
    unauthorized = _post(client, force=True)

    assert blocked.status_code == 413
    assert blocked.json()["error"]["code"] == "scan_limit_exceeded"
    assert unauthorized.status_code == 403
    assert unauthorized.json()["error"]["code"] == "force_override_not_authorized"


def test_authorized_force_can_cross_scan_and_result_caps(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    client = _client(
        tmp_path,
        max_scan_bytes=1,
        max_result_bytes=1,
        authorized_force_override=True,
    )

    response = _post(client, force=True)

    assert response.status_code == 200
    assert response.json()["forced"] is True


def test_result_cap_is_enforced_for_selected_output(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    response = _post(_client(tmp_path, max_result_bytes=1))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "result_limit_exceeded"


def test_executor_rejects_noncanonical_paths_before_duckdb(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    file = EnsuredQueryFile(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
        session_date=date(2024, 1, 18),
        object_path=f"../{path}",
        byte_size=1,
    )

    with pytest.raises(StoragePathNotAllowedError):
        BoundedDuckDBQuery(tmp_path, QueryLimits(1024, 1024)).execute(
            "SELECT * FROM data", [file]
        )


def test_executor_force_requires_authorization_and_limits_are_validated() -> None:
    with pytest.raises(ForceOverrideNotAuthorizedError):
        BoundedDuckDBQuery(Path("."), QueryLimits(1024, 1024)).execute(
            "SELECT 1", [], force=True
        )
    with pytest.raises(ValueError, match="result limit"):
        QueryLimits(10, 11)


def test_arrow_result_limit_is_measured_as_arrow_bytes(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    path = partition_path(
        session_date=date(2024, 1, 18),
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
    )
    file_size = (tmp_path / Path(*path.split("/"))).stat().st_size
    executor = BoundedDuckDBQuery(tmp_path, QueryLimits(file_size, 1))
    file = EnsuredQueryFile(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
        session_date=date(2024, 1, 18),
        object_path=path,
        byte_size=file_size,
    )

    with pytest.raises(ResultLimitExceededError):
        executor.execute("SELECT * FROM data", [file], output="arrow")
