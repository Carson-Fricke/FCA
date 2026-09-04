from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest
from azure.core.credentials import AccessToken, TokenCredential

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


class _FakeCredential(TokenCredential):
    def __init__(self, tokens: list[str]) -> None:
        self._tokens = list(tokens)
        self.audiences: list[str] = []

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        del kwargs
        self.audiences.append(scopes[0])
        return AccessToken(self._tokens.pop(0), expires_on=2_000_000_000)


def test_managed_identity_credential_attaches_audience_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer mi-token"
        return httpx.Response(
            200,
            json={
                "job_id": "ensure-aapl",
                "state": "complete",
                "created_at": "2024-01-19T21:01:00Z",
                "updated_at": "2024-01-19T21:02:00Z",
                "error_message": None,
            },
        )

    credential = _FakeCredential(["mi-token"])
    with LakeClient(
        "https://lake.test",
        credential=credential,
        audience="api-audience",
        transport=httpx.MockTransport(handler),
    ) as client:
        status = client.job("ensure-aapl")

    assert status.state is JobState.COMPLETE
    assert credential.audiences == ["api-audience"]


def test_credential_without_audience_and_audience_without_credential_fail() -> None:
    credential = _FakeCredential(["mi-token"])

    with pytest.raises(ValueError, match="both credential and"):
        LakeClient("https://lake.test", credential=credential)
    with pytest.raises(ValueError, match="both credential and"):
        LakeClient("https://lake.test", audience="api-audience")
    with pytest.raises(ValueError, match="exactly one"):
        LakeClient(
            "https://lake.test",
            "secret",
            credential=credential,
            audience="api-audience",
        )
    with pytest.raises(ValueError, match="bearer_token"):
        LakeClient("https://lake.test")


def test_401_response_is_retried_once_with_a_fresh_token() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.headers["Authorization"])
        if len(requests) == 1:
            return httpx.Response(
                401,
                json={
                    "error": {
                        "code": "unauthorized",
                        "message": "a valid bearer token is required",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "job_id": "ensure-aapl",
                "state": "complete",
                "created_at": "2024-01-19T21:01:00Z",
                "updated_at": "2024-01-19T21:02:00Z",
                "error_message": None,
            },
        )

    credential = _FakeCredential(["stale-token", "fresh-token"])
    with LakeClient(
        "https://lake.test",
        credential=credential,
        audience="api-audience",
        transport=httpx.MockTransport(handler),
    ) as client:
        status = client.job("ensure-aapl")

    assert status.state is JobState.COMPLETE
    assert requests == ["Bearer stale-token", "Bearer fresh-token"]
    assert credential.audiences == ["api-audience", "api-audience"]


def test_persistent_401_surfaces_lake_client_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "unauthorized",
                    "message": "a valid bearer token is required",
                }
            },
        )

    credential = _FakeCredential(["first-token", "second-token"])
    with LakeClient(
        "https://lake.test",
        credential=credential,
        audience="api-audience",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(LakeClientError) as caught:
            client.job("ensure-aapl")

    assert caught.value.status_code == 401
    assert caught.value.code == "unauthorized"
