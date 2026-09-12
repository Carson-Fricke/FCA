"""Deterministic offline tests for the Entra External ID OIDC verifier.

Every test serves its own generated RS256 key through an in-memory JWKS client
and pins the verifier clock, so the suite never performs network I/O and never
depends on wall-clock time.  The authorization-code exchange is exercised with
an :class:`httpx.MockTransport` token endpoint.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt import PyJWKClient, PyJWKSet
from jwt.algorithms import RSAAlgorithm

from thesis.http.auth import create_auth_router
from thesis.identity.entra import EntraOidcVerifier, EntraTokenError
from thesis.identity.models import Account, OAuthSubject, normalize_email
from thesis.identity.ports import AccountStore

TENANT_ID = "6c199962-2933-4a4b-901f-46f1578826c4"
AUDIENCE = "api://thesis-web-client"
CLIENT_ID = "thesis-web-client"
ISSUER = f"https://thesis.ciamlogin.com/{TENANT_ID}/v2.0"
OTHER_ISSUER = f"https://other.ciamlogin.com/{TENANT_ID}/v2.0"
JWKS_URL = f"https://thesis.ciamlogin.com/{TENANT_ID}/discovery/v2.0/keys"
TOKEN_URL = f"https://thesis.ciamlogin.com/{TENANT_ID}/oauth2/v2.0/token"
KID = "test-signing-key"

NOW = datetime(2026, 1, 1, tzinfo=UTC)
CREATED = datetime(2025, 12, 1, tzinfo=UTC)


def _clock() -> datetime:
    return NOW


def _seconds(moment: datetime) -> int:
    return int(moment.timestamp())


class _StaticJwksClient(PyJWKClient):
    """A PyJWKClient that serves one generated key without network access."""

    def __init__(self, public_jwk: dict[str, Any]) -> None:
        super().__init__(JWKS_URL)
        self._public_jwk = public_jwk

    def get_jwk_set(self, refresh: bool = False) -> PyJWKSet:
        del refresh
        return PyJWKSet.from_dict({"keys": [self._public_jwk]})


class _FakeAccountStore:
    """In-memory :class:`AccountStore` for the router tests."""

    def __init__(self, accounts: tuple[Account, ...] = ()) -> None:
        self._accounts = {account.id: account for account in accounts}

    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def get_by_email(self, email: str) -> Account | None:
        wanted = normalize_email(email)
        for account in self._accounts.values():
            if account.email == wanted:
                return account
        return None

    def put(self, account: Account) -> Account:
        self._accounts[account.id] = account
        return account

    def list(self) -> tuple[Account, ...]:
        return tuple(self._accounts.values())


def _private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _account(
    *,
    account_id: str = "acct-1",
    email: str = "owner@example.com",
    subjects: tuple[OAuthSubject, ...] = (),
) -> Account:
    return Account(
        id=account_id,
        email=email,
        display_name="Owner",
        oauth_subjects=subjects,
        role="owner",
        status="active",
        created_at=CREATED,
        updated_at=CREATED,
    )


def _mint(
    private_key: rsa.RSAPrivateKey,
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    nonce: str | None = None,
    exp: datetime = NOW + timedelta(hours=1),
    sub: str = "subject-1",
    **claim_overrides: Any,
) -> str:
    claims: dict[str, Any] = {
        "aud": audience,
        "iss": issuer,
        "sub": sub,
        "email": "owner@example.com",
        "name": "Owner",
        "iat": _seconds(NOW - timedelta(seconds=5)),
        "nbf": _seconds(NOW - timedelta(seconds=5)),
        "exp": _seconds(exp),
    }
    if nonce is not None:
        claims["nonce"] = nonce
    claims.update(claim_overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture()
def private_key() -> rsa.RSAPrivateKey:
    return _private_key()


@pytest.fixture()
def public_jwk(private_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    jwk = dict(json.loads(RSAAlgorithm.to_jwk(private_key.public_key())))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return jwk


@pytest.fixture()
def verifier(public_jwk: dict[str, Any]) -> EntraOidcVerifier:
    return EntraOidcVerifier(
        tenant_id=TENANT_ID,
        audience=AUDIENCE,
        issuer=ISSUER,
        jwks_url=JWKS_URL,
        token_url=TOKEN_URL,
        client_id=CLIENT_ID,
        leeway=timedelta(0),
        jwks_client=_StaticJwksClient(public_jwk),
        clock=_clock,
    )


def _client(store: AccountStore, verifier: EntraOidcVerifier) -> TestClient:
    app = FastAPI()
    app.include_router(create_auth_router(store, verifier))
    return TestClient(app)


def test_valid_id_token_is_resolved_to_its_identity(
    private_key: rsa.RSAPrivateKey, verifier: EntraOidcVerifier
) -> None:
    token = _mint(private_key)

    identity = verifier.validate_id_token(token)

    assert identity.subject == "subject-1"
    assert identity.email == "owner@example.com"
    assert identity.display_name == "Owner"


def test_issuer_mismatch_is_rejected(
    private_key: rsa.RSAPrivateKey, verifier: EntraOidcVerifier
) -> None:
    token = _mint(private_key, issuer=OTHER_ISSUER)

    with pytest.raises(EntraTokenError):
        verifier.validate_id_token(token)


def test_audience_mismatch_is_rejected(
    private_key: rsa.RSAPrivateKey, verifier: EntraOidcVerifier
) -> None:
    token = _mint(private_key, audience="api://some-other-api")

    with pytest.raises(EntraTokenError):
        verifier.validate_id_token(token)


def test_bad_signature_is_rejected(verifier: EntraOidcVerifier) -> None:
    token = _mint(_private_key())

    with pytest.raises(EntraTokenError):
        verifier.validate_id_token(token)


def test_expired_token_is_rejected(
    private_key: rsa.RSAPrivateKey, verifier: EntraOidcVerifier
) -> None:
    token = _mint(private_key, exp=NOW - timedelta(minutes=5))

    with pytest.raises(EntraTokenError):
        verifier.validate_id_token(token)


def test_nonce_mismatch_is_rejected(
    private_key: rsa.RSAPrivateKey, verifier: EntraOidcVerifier
) -> None:
    token = _mint(private_key, nonce="expected-nonce")

    with pytest.raises(EntraTokenError):
        verifier.validate_id_token(token, nonce="different-nonce")


def test_matching_nonce_is_accepted(
    private_key: rsa.RSAPrivateKey, verifier: EntraOidcVerifier
) -> None:
    token = _mint(private_key, nonce="expected-nonce")

    identity = verifier.validate_id_token(token, nonce="expected-nonce")

    assert identity.subject == "subject-1"


def test_missing_subject_is_rejected(
    private_key: rsa.RSAPrivateKey, verifier: EntraOidcVerifier
) -> None:
    token = _mint(private_key, sub="", oid="")

    with pytest.raises(EntraTokenError):
        verifier.validate_id_token(token)


def test_login_accepts_valid_token_for_known_account(
    private_key: rsa.RSAPrivateKey, verifier: EntraOidcVerifier
) -> None:
    store = _FakeAccountStore((_account(),))
    token = _mint(private_key)

    response = _client(store, verifier).post("/v1/auth/login", json={"id_token": token})

    assert response.status_code == 200
    assert response.json() == {
        "account": {
            "id": "acct-1",
            "email": "owner@example.com",
            "display_name": "Owner",
            "role": "owner",
            "status": "active",
            "oauth_subjects": [],
        }
    }
    assert "set-cookie" not in response.headers


def test_login_accepts_account_linked_by_entra_subject(
    private_key: rsa.RSAPrivateKey, verifier: EntraOidcVerifier
) -> None:
    account = _account(
        email="owner@example.com",
        subjects=(OAuthSubject(provider="entra", subject="subject-1"),),
    )
    store = _FakeAccountStore((account,))
    token = _mint(private_key, email="renamed@example.com")

    response = _client(store, verifier).post("/v1/auth/login", json={"id_token": token})

    assert response.status_code == 200
    assert response.json()["account"]["id"] == "acct-1"


def test_login_rejects_valid_token_for_unknown_account(
    private_key: rsa.RSAPrivateKey, verifier: EntraOidcVerifier
) -> None:
    store = _FakeAccountStore()
    token = _mint(private_key, exp=NOW + timedelta(hours=1))

    response = _client(store, verifier).post("/v1/auth/login", json={"id_token": token})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "account_not_found"
    assert "set-cookie" not in response.headers


def test_login_rejects_invalid_token(
    verifier: EntraOidcVerifier,
) -> None:
    store = _FakeAccountStore((_account(),))

    response = _client(store, verifier).post(
        "/v1/auth/login", json={"id_token": "not-a-token"}
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_token"
    assert "set-cookie" not in response.headers


def test_login_requires_exactly_one_credential(verifier: EntraOidcVerifier) -> None:
    store = _FakeAccountStore((_account(),))
    client = _client(store, verifier)

    assert client.post("/v1/auth/login", json={}).status_code == 422
    assert (
        client.post("/v1/auth/login", json={"code": "c", "id_token": "t"}).status_code
        == 422
    )


def test_authorization_code_exchange_uses_pkce_and_validates_id_token(
    private_key: rsa.RSAPrivateKey, public_jwk: dict[str, Any]
) -> None:
    seen: list[httpx.Request] = []
    token = _mint(private_key, nonce="nonce-1")

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id_token": token})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        verifier = EntraOidcVerifier(
            tenant_id=TENANT_ID,
            audience=AUDIENCE,
            issuer=ISSUER,
            jwks_url=JWKS_URL,
            token_url=TOKEN_URL,
            client_id=CLIENT_ID,
            leeway=timedelta(0),
            jwks_client=_StaticJwksClient(public_jwk),
            http_client=http_client,
            clock=_clock,
        )

        identity = verifier.exchange_code(
            "auth-code",
            code_verifier="pkce-verifier",
            redirect_uri="https://thesis.example.com/auth/callback",
            nonce="nonce-1",
        )

    assert identity.email == "owner@example.com"
    assert len(seen) == 1
    assert seen[0].url == httpx.URL(TOKEN_URL)
    fields = parse_qs(seen[0].content.decode("utf-8"))
    assert fields["grant_type"] == ["authorization_code"]
    assert fields["client_id"] == [CLIENT_ID]
    assert fields["code"] == ["auth-code"]
    assert fields["code_verifier"] == ["pkce-verifier"]
    assert fields["redirect_uri"] == ["https://thesis.example.com/auth/callback"]


def test_authorization_code_exchange_rejects_failed_token_endpoint(
    public_jwk: dict[str, Any],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, json={"error": "invalid_grant"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        verifier = EntraOidcVerifier(
            tenant_id=TENANT_ID,
            audience=AUDIENCE,
            issuer=ISSUER,
            jwks_url=JWKS_URL,
            token_url=TOKEN_URL,
            client_id=CLIENT_ID,
            leeway=timedelta(0),
            jwks_client=_StaticJwksClient(public_jwk),
            http_client=http_client,
            clock=_clock,
        )

        with pytest.raises(EntraTokenError):
            verifier.exchange_code(
                "auth-code",
                code_verifier="pkce-verifier",
                redirect_uri="https://thesis.example.com/auth/callback",
            )


def test_verifier_rejects_blank_configuration() -> None:
    with pytest.raises(ValueError, match="tenant id"):
        EntraOidcVerifier(tenant_id="  ", audience=AUDIENCE)
    with pytest.raises(ValueError, match="audience"):
        EntraOidcVerifier(tenant_id=TENANT_ID, audience="")
    with pytest.raises(ValueError, match="leeway"):
        EntraOidcVerifier(
            tenant_id=TENANT_ID, audience=AUDIENCE, leeway=timedelta(seconds=-1)
        )
