from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lake.app import create_app
from lake.http.auth import BearerAuthenticator, authenticate_bearer
from lake.http.models import CoverageRequest, EnsureRequest


def test_health_is_unauthenticated_and_has_probe_alias() -> None:
    client = TestClient(create_app(bearer_token="test-secret"))

    for path in ("/health", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-secret"},
        {"Authorization": "Basic test-secret"},
    ],
)
def test_protected_route_rejects_missing_or_invalid_credentials(
    headers: dict[str, str],
) -> None:
    client = TestClient(create_app(bearer_token="test-secret"))

    response = client.get("/v1/auth/check", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {
            "code": "unauthorized",
            "message": "a valid bearer token is required",
        }
    }


def test_protected_route_accepts_the_configured_bearer() -> None:
    client = TestClient(create_app(bearer_token="test-secret"))

    response = client.get(
        "/v1/auth/check",
        headers={"Authorization": "Bearer test-secret"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_auth_dependency_is_directly_testable() -> None:
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="test-secret",
    )

    assert authenticate_bearer(credentials, "test-secret") == "test-secret"
    with pytest.raises(HTTPException) as raised:
        authenticate_bearer(credentials, "different-secret")
    assert raised.value.status_code == 401
    assert "test-secret" not in repr(BearerAuthenticator("test-secret"))


def test_authenticator_fails_closed_without_a_configured_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LAKE_API__BEARER_TOKEN", raising=False)
    client = TestClient(create_app())

    response = client.get(
        "/v1/auth/check",
        headers={"Authorization": "Bearer any-value"},
    )

    assert response.status_code == 401


def test_app_reads_the_runtime_token_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAKE_API__BEARER_TOKEN", "environment-secret")
    client = TestClient(create_app())

    response = client.get(
        "/v1/auth/check",
        headers={"Authorization": "Bearer environment-secret"},
    )

    assert response.status_code == 200


def test_request_models_reject_unknown_fields_and_malformed_values() -> None:
    with pytest.raises(ValidationError):
        CoverageRequest.model_validate(
            {
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": date(2024, 1, 1),
                "end_date": date(2024, 1, 2),
                "unexpected": "value",
            }
        )

    with pytest.raises(ValidationError):
        EnsureRequest.model_validate(
            {
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": "not-a-date",
                "end_date": date(2024, 1, 2),
            }
        )


def test_validation_failures_use_the_consistent_error_shape() -> None:
    application = create_app(bearer_token="test-secret")

    @application.post("/test-validation")
    async def validation_probe(request: EnsureRequest) -> EnsureRequest:
        return request

    response = TestClient(application).post(
        "/test-validation",
        json={
            "asset_class": "equity",
            "data_type": "trade",
            "symbol": "AAPL",
            "start_date": "2024-01-01",
            "end_date": "2024-01-02",
            "unexpected": True,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["message"] == "request validation failed"


def test_request_models_validate_date_order_and_symbol_shape() -> None:
    with pytest.raises(ValidationError, match="end_date"):
        EnsureRequest.model_validate(
            {
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": date(2024, 1, 2),
                "end_date": date(2024, 1, 1),
            }
        )

    with pytest.raises(ValidationError):
        EnsureRequest.model_validate(
            {
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "../AAPL",
                "start_date": date(2024, 1, 1),
                "end_date": date(2024, 1, 2),
            }
        )
