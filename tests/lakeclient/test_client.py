from __future__ import annotations

from datetime import date

import httpx
import pytest

from lakeclient import (
    AssetClass,
    CoverageResponse,
    DatasetRequest,
    DataType,
    EnsureAccepted,
    JobPollingTimeout,
    JobState,
    LakeClient,
    LakeClientError,
    LakeProtocolError,
)


def _request() -> DatasetRequest:
    return DatasetRequest(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.QUOTE,
        symbol="AAPL",
        start_date=date(2024, 1, 19),
        end_date=date(2024, 1, 19),
    )


def test_ensure_uses_bearer_auth_and_parses_satisfied_coverage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        assert request.url.path == "/v1/catalog/ensure"
        assert request.read() == (
            b'{"asset_class":"equity","data_type":"quote",'
            b'"start_date":"2024-01-19","end_date":"2024-01-19",'
            b'"symbol":"AAPL"}'
        )
        return httpx.Response(
            200,
            json={
                "asset_class": "equity",
                "data_type": "quote",
                "symbol": "AAPL",
                "start_date": "2024-01-19",
                "end_date": "2024-01-19",
                "entries": [
                    {
                        "session_date": "2024-01-19",
                        "open_time": "2024-01-19T14:30:00Z",
                        "close_time": "2024-01-19T21:00:00Z",
                        "is_partial": False,
                        "status": "complete",
                        "object_path": "aapl.parquet",
                        "row_count": 2,
                        "byte_size": 100,
                        "checksum": "sha256",
                        "error_message": None,
                    }
                ],
            },
        )

    with LakeClient(
        "https://lake.test", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        response = client.ensure(_request())

    assert isinstance(response, CoverageResponse)
    assert response.symbol == "AAPL"
    assert response.entries[0].open_time.isoformat() == "2024-01-19T14:30:00+00:00"
    assert response.entries[0].row_count == 2


def test_ensure_and_job_polling_parse_asynchronous_states() -> None:
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.url.path == "/v1/catalog/ensure":
            return httpx.Response(
                202,
                headers={"Location": "/v1/jobs/ensure-aapl"},
                json={"job_id": "ensure-aapl", "state": "queued"},
            )
        poll_count += 1
        state = "running" if poll_count == 1 else "complete"
        return httpx.Response(
            200,
            json={
                "job_id": "ensure-aapl",
                "state": state,
                "created_at": "2024-01-19T21:01:00Z",
                "updated_at": "2024-01-19T21:02:00Z",
                "error_message": None,
            },
        )

    with LakeClient(
        "https://lake.test", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        accepted = client.ensure(_request())
        assert isinstance(accepted, EnsureAccepted)
        status = client.wait_for_job(
            accepted.job_id,
            timeout=1,
            poll_interval=0,
        )

    assert accepted.location == "/v1/jobs/ensure-aapl"
    assert status.state is JobState.COMPLETE
    assert status.succeeded
    assert poll_count == 2


def test_polling_timeout_preserves_last_job_status() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "job_id": request.url.path.rsplit("/", 1)[-1],
                "state": "queued",
                "created_at": "2024-01-19T21:01:00Z",
                "updated_at": "2024-01-19T21:01:00Z",
                "error_message": None,
            },
        )
    )

    with LakeClient("https://lake.test", "secret", transport=transport) as client:
        with pytest.raises(JobPollingTimeout) as caught:
            client.wait_for_job("ensure-aapl", timeout=0)

    assert caught.value.job_id == "ensure-aapl"
    assert caught.value.last_status.state is JobState.QUEUED


def test_api_errors_keep_status_code_and_structured_details() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            409,
            json={
                "error": {
                    "code": "coverage_incomplete",
                    "message": "coverage is incomplete",
                    "details": {"sessions": ["2024-01-19"]},
                }
            },
        )
    )

    with LakeClient("https://lake.test", "secret", transport=transport) as client:
        with pytest.raises(LakeClientError) as caught:
            client.query_files(_request())

    assert caught.value.status_code == 409
    assert caught.value.code == "coverage_incomplete"
    assert caught.value.details == {"sessions": ["2024-01-19"]}


def test_malformed_success_response_is_a_protocol_error() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"unexpected": True})
    )

    with LakeClient("https://lake.test", "secret", transport=transport) as client:
        with pytest.raises(LakeProtocolError, match="invalid ensure response"):
            client.ensure(_request())


def test_dataset_request_validates_asset_specific_selection() -> None:
    option = DatasetRequest(
        asset_class=AssetClass.OPTION,
        data_type=DataType.TRADE,
        start_date=date(2024, 1, 19),
        end_date=date(2024, 1, 19),
        underlying=" SPY ",
        contract_symbols=(" SPY240119C00500000 ",),
    )

    assert option.to_payload()["underlying"] == "SPY"
    assert option.to_payload()["contract_symbols"] == ["SPY240119C00500000"]
    with pytest.raises(ValueError, match="symbol is required"):
        DatasetRequest(
            asset_class=AssetClass.EQUITY,
            data_type=DataType.QUOTE,
            start_date=date(2024, 1, 19),
            end_date=date(2024, 1, 19),
        )
