"""Entra OIDC login exchange for the Thesis API.

The router exposes ``POST /v1/auth/login``.  It accepts either an Entra
authorization code (exchanged with PKCE at the injected token endpoint) or an
already-issued ID token, verifies the identity through
:class:`~thesis.identity.entra.EntraOidcVerifier`, resolves an app-owned account
in the injected :class:`~thesis.identity.ports.AccountStore`, and returns that
account's identity.

Account resolution is invite-only by construction: the identity is looked up by
normalized email first and then by a linked Entra OAuth subject; when no
account matches, the request is rejected.  The router issues no session or
cookie of any kind -- session issuance is deliberately a separate concern owned
by the composition layer.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, model_validator

from thesis.identity.entra import EntraIdentity, EntraOidcVerifier, EntraTokenError
from thesis.identity.models import Account, normalize_email
from thesis.identity.ports import AccountStore


class AuthLoginRequest(BaseModel):
    """One Entra login result: an authorization code or an ID token."""

    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    id_token: str | None = None
    code_verifier: str | None = None
    redirect_uri: str | None = None
    nonce: str | None = None

    @model_validator(mode="after")
    def _one_credential_with_pkce_inputs(self) -> AuthLoginRequest:
        provided = (self.code is not None, self.id_token is not None)
        if sum(provided) != 1:
            raise ValueError("provide exactly one of code or id_token")
        if self.code is not None and (
            self.code_verifier is None or self.redirect_uri is None
        ):
            raise ValueError("code exchange requires code_verifier and redirect_uri")
        return self


def _account_payload(account: Account) -> dict[str, object]:
    """Shape one account for the login response (identity only, no session)."""
    return {
        "id": account.id,
        "email": account.email,
        "display_name": account.display_name,
        "role": account.role,
        "status": account.status,
        "oauth_subjects": [
            {"provider": link.provider, "subject": link.subject}
            for link in account.oauth_subjects
        ],
    }


def _resolve_account(store: AccountStore, identity: EntraIdentity) -> Account | None:
    """Return the app-owned account for ``identity``, or ``None``.

    A normalized-email match is authoritative; otherwise an account that has
    already linked this Entra subject is returned.  An identity with neither is
    unprovisioned and must be rejected by the caller.
    """
    if identity.email is not None:
        try:
            normalized = normalize_email(identity.email)
        except ValueError:
            normalized = None
        if normalized is not None:
            account = store.get_by_email(normalized)
            if account is not None:
                return account
    for account in store.list():
        for link in account.oauth_subjects:
            if link.provider == "entra" and link.subject == identity.subject:
                return account
    return None


def create_auth_router(
    account_store: AccountStore,
    verifier: EntraOidcVerifier,
) -> APIRouter:
    """Create the Entra login router over the injected store and verifier.

    The router is intentionally not protected by the shared bearer/Entra
    dependency: it is the credential-establishing endpoint.  It only resolves
    and returns an existing account; the caller that composes the application
    attaches the session cookie.
    """
    router = APIRouter(prefix="/v1/auth", tags=["auth"])

    @router.post("/login")
    async def login(payload: AuthLoginRequest) -> dict[str, object]:
        try:
            if payload.code is not None:
                if payload.code_verifier is None or payload.redirect_uri is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "code": "invalid_request",
                            "message": "code exchange requires code_verifier and redirect_uri",
                        },
                    )
                identity = verifier.exchange_code(
                    payload.code,
                    code_verifier=payload.code_verifier,
                    redirect_uri=payload.redirect_uri,
                    nonce=payload.nonce,
                )
            else:
                identity = verifier.validate_id_token(
                    payload.id_token or "", nonce=payload.nonce
                )
        except EntraTokenError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "invalid_token",
                    "message": "the Entra sign-in could not be validated",
                },
            ) from error

        account = _resolve_account(account_store, identity)
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "account_not_found",
                    "message": "no Thesis account is provisioned for this identity",
                },
            )
        return {"account": _account_payload(account)}

    return router


__all__ = [
    "AuthLoginRequest",
    "create_auth_router",
]
