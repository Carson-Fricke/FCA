"""Focused tests for Entra managed-identity JWT authentication."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from jwt import PyJWKClient, PyJWKSet
from jwt.algorithms import RSAAlgorithm

from lake.app import create_app
from lake.http.auth import (
    BearerAuthenticator,
    CombinedAuthenticator,
    EntraJwtAuthenticator,
    authenticate_bearer,
)

TENANT_ID = "6c199962-2933-4a4b-901f-46f1578826c4"
AUDIENCE = "cbc572d2-ae2e-4501-af76-0515e2f73bcd"
HARNESS_AUDIENCE = "c380c885-4a51-4b0d-9dc7-2f0c2fc70705"
ROLE = "Lake.Access"
V1_ISSUER = f"https://sts.windows.net/{TENANT_ID}/"
V2_ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
OTHER_TENANT_ISSUER = "https://sts.windows.net/22222222-2222-2222-2222-222222222222/"
KID = "test-signing-key"


def _private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _StaticJwksClient(PyJWKClient):
    """A PyJWKClient that serves one generated key without network access."""

    def __init__(self, public_jwk: dict[str, Any]) -> None:
        super().__init__(
            f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
        )
        self._public_jwk = public_jwk

    def get_jwk_set(self, refresh: bool = False) -> PyJWKSet:
        del refresh
        return PyJWKSet.from_dict({"keys": [self._public_jwk]})


@pytest.fixture()
def private_key() -> rsa.RSAPrivateKey:
    return _private_key()


@pytest.fixture()
def public_jwk(private_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    jwk = dict(json.loads(RSAAlgorithm.to_jwk(private_key.public_key())))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return jwk


@pytest.fixture()
def authenticator(
    private_key: rsa.RSAPrivateKey, public_jwk: dict[str, Any]
) -> EntraJwtAuthenticator:
    return EntraJwtAuthenticator(
        tenant_id=TENANT_ID,
        audience=AUDIENCE,
        role=ROLE,
        jwks_client=_StaticJwksClient(public_jwk),
    )


def _mint(
    private_key: rsa.RSAPrivateKey,
    *,
    issuer: str = V1_ISSUER,
    audience: str = AUDIENCE,
    roles: list[str] | None = None,
    **claim_overrides: Any,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "aud": audience,
        "iss": issuer,
        "sub": "00000000-0000-0000-0000-000000000000",
        "appid": "11111111-1111-1111-1111-111111111111",
        "roles": [ROLE] if roles is None else roles,
        "iat": now - timedelta(seconds=5),
        "nbf": now - timedelta(seconds=5),
        "exp": now + timedelta(minutes=60),
    }
    claims.update(claim_overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": KID})


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _combined(public_jwk: dict[str, Any]) -> CombinedAuthenticator:
    return CombinedAuthenticator(
        "static-secret",
        entra=EntraJwtAuthenticator(
            tenant_id=TENANT_ID,
            audience=AUDIENCE,
            jwks_client=_StaticJwksClient(public_jwk),
        ),
    )


def test_valid_v1_issuer_token_is_accepted(
    private_key: rsa.RSAPrivateKey, authenticator: EntraJwtAuthenticator
) -> None:
    token = _mint(private_key)

    assert authenticator.authenticate(token) == token


def test_valid_v2_issuer_token_is_accepted(
    private_key: rsa.RSAPrivateKey, authenticator: EntraJwtAuthenticator
) -> None:
    token = _mint(private_key, issuer=V2_ISSUER)

    assert authenticator.authenticate(token) == token


def test_single_string_role_claim_is_accepted(
    private_key: rsa.RSAPrivateKey, authenticator: EntraJwtAuthenticator
) -> None:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "aud": AUDIENCE,
            "iss": V1_ISSUER,
            "roles": ROLE,
            "iat": now,
            "exp": now + timedelta(minutes=60),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": KID},
    )

    assert authenticator.authenticate(token) == token


def test_wrong_audience_is_rejected(
    private_key: rsa.RSAPrivateKey, authenticator: EntraJwtAuthenticator
) -> None:
    token = _mint(private_key, audience=HARNESS_AUDIENCE)

    with pytest.raises(HTTPException) as raised:
        authenticator.authenticate(token)

    assert raised.value.status_code == 401


def test_wrong_role_is_rejected(
    private_key: rsa.RSAPrivateKey, authenticator: EntraJwtAuthenticator
) -> None:
    token = _mint(private_key, roles=["Harness.Access"])

    with pytest.raises(HTTPException) as raised:
        authenticator.authenticate(token)

    assert raised.value.status_code == 401


def test_missing_role_is_rejected(
    private_key: rsa.RSAPrivateKey, authenticator: EntraJwtAuthenticator
) -> None:
    token = _mint(private_key, roles=[])

    with pytest.raises(HTTPException) as raised:
        authenticator.authenticate(token)

    assert raised.value.status_code == 401


def test_expired_token_is_rejected(
    private_key: rsa.RSAPrivateKey, authenticator: EntraJwtAuthenticator
) -> None:
    token = _mint(
        private_key,
        iat=datetime.now(UTC) - timedelta(hours=2),
        nbf=datetime.now(UTC) - timedelta(hours=2),
        exp=datetime.now(UTC) - timedelta(minutes=5),
    )

    with pytest.raises(HTTPException) as raised:
        authenticator.authenticate(token)

    assert raised.value.status_code == 401


def test_issuer_from_another_tenant_is_rejected(
    private_key: rsa.RSAPrivateKey, authenticator: EntraJwtAuthenticator
) -> None:
    token = _mint(private_key, issuer=OTHER_TENANT_ISSUER)

    with pytest.raises(HTTPException) as raised:
        authenticator.authenticate(token)

    assert raised.value.status_code == 401


def test_tampered_signature_is_rejected(
    private_key: rsa.RSAPrivateKey, authenticator: EntraJwtAuthenticator
) -> None:
    token = _mint(private_key)
    header, payload, signature = token.split(".")
    # Tamper a mid-string character: the final base64 character can carry
    # only padding bits, so flipping it may decode to the identical bytes.
    middle = len(signature) // 2
    tampered = "A" if signature[middle] != "A" else "B"
    token = f"{header}.{payload}.{signature[:middle]}{tampered}{signature[middle + 1 :]}"

    with pytest.raises(HTTPException) as raised:
        authenticator.authenticate(token)

    assert raised.value.status_code == 401


def test_token_signed_by_another_key_is_rejected(
    public_jwk: dict[str, Any],
) -> None:
    authenticator = EntraJwtAuthenticator(
        tenant_id=TENANT_ID,
        audience=AUDIENCE,
        jwks_client=_StaticJwksClient(public_jwk),
    )
    token = _mint(_private_key())

    with pytest.raises(HTTPException):
        authenticator.authenticate(token)


def test_malformed_credential_is_rejected(
    authenticator: EntraJwtAuthenticator,
) -> None:
    with pytest.raises(HTTPException):
        authenticator.authenticate("not-a-token")


def test_combined_authenticator_accepts_valid_entra_jwt(
    public_jwk: dict[str, Any], private_key: rsa.RSAPrivateKey
) -> None:
    token = _mint(private_key)

    assert asyncio.run(_combined(public_jwk)(_credentials(token))) == token


def test_combined_authenticator_accepts_static_bearer_alongside_entra(
    public_jwk: dict[str, Any],
) -> None:
    assert asyncio.run(_combined(public_jwk)(_credentials("static-secret"))) == (
        "static-secret"
    )


def test_combined_authenticator_fails_closed_for_invalid_entra_jwt(
    public_jwk: dict[str, Any],
) -> None:
    with pytest.raises(HTTPException):
        asyncio.run(_combined(public_jwk)(_credentials("aaa.bbb.ccc")))


def test_combined_without_entra_matches_bearer_behavior() -> None:
    combined = CombinedAuthenticator("static-secret")
    bearer = BearerAuthenticator("static-secret")

    assert (
        asyncio.run(combined(_credentials("static-secret")))
        == asyncio.run(bearer(_credentials("static-secret")))
        == "static-secret"
    )
    with pytest.raises(HTTPException):
        asyncio.run(combined(_credentials("wrong-secret")))
    with pytest.raises(HTTPException):
        asyncio.run(combined(None))


def test_authenticate_bearer_error_shape_is_stable() -> None:
    with pytest.raises(HTTPException) as raised:
        authenticate_bearer(_credentials("wrong"), "expected")
    exception = raised.value
    assert exception.status_code == 401
    assert exception.detail == {
        "code": "unauthorized",
        "message": "a valid bearer token is required",
    }
    assert exception.headers == {"WWW-Authenticate": "Bearer"}


@pytest.fixture()
def app_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKE_API__AUTH__ENTRA_TENANT_ID", TENANT_ID)
    monkeypatch.setenv("LAKE_API__AUTH__ENTRA_AUDIENCE", AUDIENCE)


def _patch_jwks(monkeypatch: pytest.MonkeyPatch, public_jwk: dict[str, Any]) -> None:
    monkeypatch.setattr(
        "lake.http.auth.PyJWKClient",
        lambda url: _StaticJwksClient(public_jwk),
    )


def test_app_accepts_valid_entra_jwt_without_static_token(
    monkeypatch: pytest.MonkeyPatch,
    private_key: rsa.RSAPrivateKey,
    public_jwk: dict[str, Any],
    app_environ: None,
) -> None:
    del app_environ
    _patch_jwks(monkeypatch, public_jwk)
    monkeypatch.delenv("LAKE_API__BEARER_TOKEN", raising=False)
    client = TestClient(create_app())

    response = client.get(
        "/v1/auth/check",
        headers={"Authorization": f"Bearer {_mint(private_key)}"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_accepts_static_bearer_when_entra_configured(
    monkeypatch: pytest.MonkeyPatch,
    public_jwk: dict[str, Any],
    app_environ: None,
) -> None:
    del app_environ
    _patch_jwks(monkeypatch, public_jwk)
    monkeypatch.setenv("LAKE_API__BEARER_TOKEN", "transition-secret")
    client = TestClient(create_app())

    response = client.get(
        "/v1/auth/check",
        headers={"Authorization": "Bearer transition-secret"},
    )

    assert response.status_code == 200


def _rejected_entra_credentials() -> list[str]:
    key = _private_key()
    return [
        _mint(key, audience=HARNESS_AUDIENCE),
        _mint(key, roles=["Harness.Access"]),
        _mint(key, roles=[]),
        _mint(
            key,
            iat=datetime.now(UTC) - timedelta(hours=2),
            nbf=datetime.now(UTC) - timedelta(hours=2),
            exp=datetime.now(UTC) - timedelta(minutes=5),
        ),
        _mint(key, issuer=OTHER_TENANT_ISSUER),
        "aaa.bbb.ccc",
    ]


def test_app_rejects_invalid_entra_credentials_with_the_stable_error(
    monkeypatch: pytest.MonkeyPatch,
    public_jwk: dict[str, Any],
    app_environ: None,
) -> None:
    del app_environ
    _patch_jwks(monkeypatch, public_jwk)
    monkeypatch.setenv("LAKE_API__BEARER_TOKEN", "transition-secret")
    client = TestClient(create_app())

    for credential in _rejected_entra_credentials():
        response = client.get(
            "/v1/auth/check",
            headers={"Authorization": f"Bearer {credential}"},
        )

        assert response.status_code == 401, credential
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json() == {
            "error": {
                "code": "unauthorized",
                "message": "a valid bearer token is required",
            }
        }


def test_app_health_stays_unauthenticated_when_entra_configured(
    monkeypatch: pytest.MonkeyPatch,
    public_jwk: dict[str, Any],
    app_environ: None,
) -> None:
    del app_environ
    _patch_jwks(monkeypatch, public_jwk)
    client = TestClient(create_app(bearer_token="transition-secret"))

    for path in ("/health", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_app_without_entra_configuration_keeps_bearer_only(
    monkeypatch: pytest.MonkeyPatch,
    private_key: rsa.RSAPrivateKey,
) -> None:
    monkeypatch.delenv("LAKE_API__AUTH__ENTRA_TENANT_ID", raising=False)
    monkeypatch.delenv("LAKE_API__AUTH__ENTRA_AUDIENCE", raising=False)
    monkeypatch.setenv("LAKE_API__BEARER_TOKEN", "static-secret")
    client = TestClient(create_app())

    response = client.get(
        "/v1/auth/check",
        headers={"Authorization": "Bearer static-secret"},
    )
    assert response.status_code == 200

    rejected = client.get(
        "/v1/auth/check",
        headers={"Authorization": f"Bearer {_mint(private_key)}"},
    )
    assert rejected.status_code == 401


def test_blank_entra_environment_values_disable_entra(
    monkeypatch: pytest.MonkeyPatch,
    public_jwk: dict[str, Any],
) -> None:
    _patch_jwks(monkeypatch, public_jwk)
    monkeypatch.setenv("LAKE_API__AUTH__ENTRA_TENANT_ID", "   ")
    monkeypatch.setenv("LAKE_API__AUTH__ENTRA_AUDIENCE", "")
    monkeypatch.setenv("LAKE_API__BEARER_TOKEN", "static-secret")
    client = TestClient(create_app())

    response = client.get(
        "/v1/auth/check",
        headers={"Authorization": "Bearer static-secret"},
    )

    assert response.status_code == 200


def test_entra_authenticator_rejects_blank_configuration() -> None:
    with pytest.raises(ValueError, match="tenant id"):
        EntraJwtAuthenticator(tenant_id="  ", audience=AUDIENCE)
    with pytest.raises(ValueError, match="audience"):
        EntraJwtAuthenticator(tenant_id=TENANT_ID, audience="")
    with pytest.raises(ValueError, match="role"):
        EntraJwtAuthenticator(tenant_id=TENANT_ID, audience=AUDIENCE, role="")
