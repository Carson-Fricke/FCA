"""Deterministic tests for per-request workspace authorization (T308).

The suite composes the workspace router with offline fakes: a header-driven
authenticator standing in for the composed bearer/Entra/session authenticator,
a fixed owner directory standing in for the workspace catalog, and an
account-to-owner mapping standing in for the T312 owner-seeding seam.  No
network, clock, or real catalog is involved.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from thesis.auth import WorkspaceAuthorizer
from thesis.auth.workspace_access import (
    CatalogWorkspaceOwnerLookup,
    WorkspaceOwnerLookup,
    principal_account_id,
)
from thesis.http.workspaces import (
    WorkspaceCatalogStore,
    WorkspaceDataService,
    create_workspaces_router,
    manifest_owner_lookup,
)

WORKSPACE = "alpha-lab"
OWNER_ACCOUNT = "acct-owner"
INTRUDER_ACCOUNT = "acct-intruder"
OWNER_ID = "owner-1"

OWNER_TOKEN = "owner-token"
INTRUDER_TOKEN = "intruder-token"
SESSION_TOKEN = "session-cookie-token"

_SNAPSHOT_URL = f"/v1/workspaces/{WORKSPACE}/snapshot"
_UNKNOWN_SNAPSHOT_URL = "/v1/workspaces/ghost/snapshot"


class FakeAuthenticator:
    """Offline stand-in for the composed bearer/Entra/session authenticator."""

    def __init__(self, principals: Mapping[str, str]) -> None:
        self._principals = dict(principals)

    async def __call__(self, request: Request) -> str:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})
        principal = self._principals.get(header[len("Bearer ") :])
        if principal is None:
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})
        return principal


def _owner_lookup(owners: Mapping[str, str]) -> WorkspaceOwnerLookup:
    def lookup(workspace_id: str) -> str | None:
        return owners.get(workspace_id)

    return lookup


def _account_owner(accounts: Mapping[str, str]) -> Callable[[str], str]:
    def mapping(account_id: str) -> str:
        return accounts.get(account_id, account_id)

    return mapping


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _client() -> TestClient:
    service = WorkspaceDataService(WorkspaceCatalogStore())
    workspace = service.create_workspace("Alpha Lab")
    assert workspace["id"] == WORKSPACE

    authenticator = FakeAuthenticator(
        {
            OWNER_TOKEN: OWNER_ACCOUNT,
            INTRUDER_TOKEN: INTRUDER_ACCOUNT,
            SESSION_TOKEN: f"session:{OWNER_ACCOUNT}",
        }
    )
    authorizer = WorkspaceAuthorizer(_owner_lookup({WORKSPACE: OWNER_ID}))
    router = create_workspaces_router(
        authenticator=authenticator,
        authorizer=authorizer,
        account_owner=_account_owner(
            {OWNER_ACCOUNT: OWNER_ID, INTRUDER_ACCOUNT: "owner-2"}
        ),
    )

    app = FastAPI()
    app.state.workspace_service = service
    app.include_router(router, dependencies=[Depends(authenticator)])
    return TestClient(app)


def test_owner_account_can_read_its_workspace() -> None:
    response = _client().get(_SNAPSHOT_URL, headers=_headers(OWNER_TOKEN))

    assert response.status_code == 200
    assert response.json()["overview"]["workspace"]["id"] == WORKSPACE


def test_session_principal_is_derived_to_its_account_id() -> None:
    response = _client().get(_SNAPSHOT_URL, headers=_headers(SESSION_TOKEN))

    assert response.status_code == 200


def test_authenticated_account_without_ownership_is_forbidden() -> None:
    response = _client().get(_SNAPSHOT_URL, headers=_headers(INTRUDER_TOKEN))

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "workspace_forbidden"


def test_request_without_credentials_is_unauthorized() -> None:
    response = _client().get(_SNAPSHOT_URL)

    assert response.status_code == 401


def test_unknown_workspace_fails_closed() -> None:
    response = _client().get(_UNKNOWN_SNAPSHOT_URL, headers=_headers(OWNER_TOKEN))

    assert response.status_code == 403


def test_collection_route_requires_authentication_only() -> None:
    client = _client()

    assert (
        client.get("/v1/workspaces", headers=_headers(OWNER_TOKEN)).status_code == 200
    )
    assert client.get("/v1/workspaces").status_code == 401


def test_principal_account_id_unwraps_session_principals() -> None:
    assert principal_account_id(f"session:{OWNER_ACCOUNT}") == OWNER_ACCOUNT
    assert principal_account_id("raw-bearer-principal") == "raw-bearer-principal"
    assert principal_account_id("") is None
    assert principal_account_id("session:") is None


def test_catalog_owner_lookup_delegates_and_fails_closed() -> None:
    lookup = CatalogWorkspaceOwnerLookup(
        lambda workspace_id: OWNER_ID if workspace_id == WORKSPACE else None
    )

    assert lookup(WORKSPACE) == OWNER_ID
    assert lookup("ghost") is None


def test_manifest_owner_lookup_reads_summary_owner() -> None:
    store = WorkspaceCatalogStore()
    store.add({"id": WORKSPACE, "owner": {"owner_id": OWNER_ID}})
    lookup = manifest_owner_lookup(store)

    assert lookup(WORKSPACE) == OWNER_ID
    assert lookup("ghost") is None
