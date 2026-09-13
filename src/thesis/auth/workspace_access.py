"""Workspace ownership checks for operator sessions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from thesis.auth.session import OperatorSession

#: Prefix :class:`~thesis.http.auth.SessionCookieAuthenticator` put on the
#: principal it returns for a cookie session.
_SESSION_PRINCIPAL_PREFIX = "session:"

#: Maps an authenticated account id onto the workspace owner it acts as.
AccountOwnerMap = Callable[[str], str]


def principal_account_id(principal: str) -> str | None:
    """Derive the account id from an authentication principal.

    Cookie sessions arrive as ``"session:<account_id>"``; the static bearer and
    Entra JWT paths return their own principal identifiers unchanged.  An empty
    principal has no identity and is reported as ``None`` so callers fail
    closed.
    """
    if not principal:
        return None
    if principal.startswith(_SESSION_PRINCIPAL_PREFIX):
        account_id = principal[len(_SESSION_PRINCIPAL_PREFIX) :]
        return account_id or None
    return principal


def identity_account_owner(account_id: str) -> str:
    """Default owner mapping for the seeded owner.

    The seeded owner's account id equals the workspace ``owner_id``; T312's
    owner seeding refines this mapping without changing the call sites.
    """
    return account_id


class WorkspaceAuthorizationError(PermissionError):
    """Raised without disclosing whether a different workspace exists."""


class WorkspaceOwnerLookup(Protocol):
    """Resolve the owner identifier at the workspace service boundary."""

    def __call__(self, workspace_id: str) -> str | None: ...


class CatalogWorkspaceOwnerLookup:
    """Owner lookup backed by an injected catalog/manifest accessor.

    ``owner_of`` is the seam onto the workspace catalog: it returns the
    manifest ``owner.owner_id`` for a known workspace and ``None`` for an
    unknown or unowned one, so the authorizer fails closed.  Keeping the read
    behind a callable lets tests inject a fake and keeps the auth package free
    of workspace imports.
    """

    def __init__(self, owner_of: Callable[[str], str | None]) -> None:
        self._owner_of = owner_of

    def __call__(self, workspace_id: str) -> str | None:
        return self._owner_of(workspace_id)


class WorkspaceAuthorizer:
    """Enforce selected-workspace and owner identity boundaries."""

    def __init__(self, owner_lookup: WorkspaceOwnerLookup) -> None:
        self._owner_lookup = owner_lookup

    def authorize_operator(self, operator_id: str, workspace_id: str) -> None:
        """Require the operator to own the selected workspace."""
        if not operator_id or not workspace_id:
            raise WorkspaceAuthorizationError("workspace access denied")
        if self._owner_lookup(workspace_id) != operator_id:
            raise WorkspaceAuthorizationError("workspace access denied")

    def authorize_session(
        self, session: OperatorSession, requested_workspace_id: str
    ) -> None:
        """Reject cross-workspace reuse and recheck current ownership."""
        if requested_workspace_id != session.workspace_id:
            raise WorkspaceAuthorizationError("workspace access denied")
        self.authorize_operator(session.operator_id, requested_workspace_id)


__all__ = [
    "AccountOwnerMap",
    "CatalogWorkspaceOwnerLookup",
    "WorkspaceAuthorizationError",
    "WorkspaceAuthorizer",
    "WorkspaceOwnerLookup",
    "identity_account_owner",
    "principal_account_id",
]
