"""Workspace ownership checks for operator sessions."""

from __future__ import annotations

from typing import Protocol

from thesis.auth.session import OperatorSession


class WorkspaceAuthorizationError(PermissionError):
    """Raised without disclosing whether a different workspace exists."""


class WorkspaceOwnerLookup(Protocol):
    """Resolve the owner identifier at the workspace service boundary."""

    def __call__(self, workspace_id: str) -> str | None: ...


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
    "WorkspaceAuthorizationError",
    "WorkspaceAuthorizer",
    "WorkspaceOwnerLookup",
]
