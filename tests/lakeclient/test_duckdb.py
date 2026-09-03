from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import httpx

from lakeclient import (
    AssetClass,
    DatasetRequest,
    DataType,
    EnsureAccepted,
    FileQueryState,
    JobState,
    LakeClient,
    nbbo_spread,
    read_files,
)


def _write_quotes(path: Path) -> None:
    with duckdb.connect(database=":memory:") as connection:
        connection.sql(
            """
            SELECT
                TIMESTAMPTZ '2024-01-19 14:30:00+00' AS timestamp,
                'AAPL'::VARCHAR AS symbol,
                'XNAS'::VARCHAR AS bid_exchange,
                189.120000000000000000::DECIMAL(38, 18) AS bid_price,
                100::UINTEGER AS bid_size,
                'XNYS'::VARCHAR AS ask_exchange,
                189.140000000000000000::DECIMAL(38, 18) AS ask_price,
                120::UINTEGER AS ask_size,
                ['R']::VARCHAR[] AS conditions,
                'C'::VARCHAR AS tape
            """
        ).write_parquet(str(path))


def test_local_aapl_snippet_ensures_polls_and_computes_nbbo_spread(
    tmp_path: Path,
) -> None:
    """Exercise the documented local flow with no provider or network access."""
    parquet_path = tmp_path / "aapl-quotes.parquet"
    _write_quotes(parquet_path)
    poll_count = 0
    seen_requests: list[str] = []

    def local_api(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        assert request.headers["Authorization"] == "Bearer local-token"
        seen_requests.append(request.url.path)
        if request.url.path == "/v1/catalog/ensure":
            return httpx.Response(
                202,
                headers={"Location": "/v1/jobs/ensure-aapl-quotes"},
                json={"job_id": "ensure-aapl-quotes", "state": "queued"},
            )
        if request.url.path == "/v1/jobs/ensure-aapl-quotes":
            poll_count += 1
            return httpx.Response(
                200,
                json={
                    "job_id": "ensure-aapl-quotes",
                    "state": "running" if poll_count == 1 else "complete",
                    "created_at": "2024-01-19T21:01:00Z",
                    "updated_at": "2024-01-19T21:02:00Z",
                    "error_message": None,
                },
            )
        assert request.url.path == "/v1/query"
        return httpx.Response(
            200,
            json={
                "files": [
                    {
                        "asset_class": "equity",
                        "data_type": "quote",
                        "symbol": "AAPL",
                        "session_date": "2024-01-19",
                        "object_path": (
                            "date=2024-01-19/asset_class=equity/"
                            "data_type=quote/AAPL.parquet"
                        ),
                        "url": parquet_path.as_uri(),
                        "sas_url": parquet_path.as_uri(),
                        "row_count": 1,
                        "byte_size": parquet_path.stat().st_size,
                        "checksum": "local",
                    }
                ],
                "results": {
                    "requested_sessions": 1,
                    "returned_files": 1,
                    "complete_sessions": 1,
                    "pending_sessions": 0,
                    "state": "complete",
                },
                "limitations": ["local mocked API response"],
            },
        )

    request = DatasetRequest(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.QUOTE,
        symbol="AAPL",
        start_date=date(2024, 1, 19),
        end_date=date(2024, 1, 19),
    )
    with LakeClient(
        "https://lake.local",
        "local-token",
        transport=httpx.MockTransport(local_api),
    ) as client:
        accepted = client.ensure(request)
        assert isinstance(accepted, EnsureAccepted)
        job = client.wait_for_job(accepted.job_id, timeout=1, poll_interval=0)
        assert job.state is JobState.COMPLETE
        files = client.query_files(request)

    assert files.results.state is FileQueryState.COMPLETE
    spread_rows = nbbo_spread(files.files).project("nbbo_spread").fetchall()
    assert spread_rows == [(Decimal("0.020000000000000000"),)]
    assert seen_requests == [
        "/v1/catalog/ensure",
        "/v1/jobs/ensure-aapl-quotes",
        "/v1/jobs/ensure-aapl-quotes",
        "/v1/query",
    ]


def test_duckdb_reader_uses_sas_url_and_accepts_an_injected_connection(
    tmp_path: Path,
) -> None:
    parquet_path = tmp_path / "aapl-quotes.parquet"
    _write_quotes(parquet_path)
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "files": [
                    {
                        "asset_class": "equity",
                        "data_type": "quote",
                        "symbol": "AAPL",
                        "session_date": "2024-01-19",
                        "object_path": "AAPL.parquet",
                        "url": "not-the-reader-value",
                        "sas_url": parquet_path.as_uri(),
                        "row_count": 1,
                        "byte_size": parquet_path.stat().st_size,
                        "checksum": None,
                    }
                ],
                "results": {
                    "requested_sessions": 1,
                    "returned_files": 1,
                    "complete_sessions": 1,
                    "pending_sessions": 0,
                    "state": "complete",
                },
                "limitations": [],
            },
        )

    request = DatasetRequest(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.QUOTE,
        symbol="AAPL",
        start_date=date(2024, 1, 19),
        end_date=date(2024, 1, 19),
    )
    with LakeClient(
        "https://lake.test", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        files = client.query_files(request)
    with duckdb.connect(database=":memory:") as connection:
        symbols = (
            read_files(files.files, connection=connection).project("symbol").fetchall()
        )

    assert symbols == [("AAPL",)]
    assert request_count == 1
