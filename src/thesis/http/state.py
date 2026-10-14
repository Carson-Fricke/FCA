"""Workspace-state HTTP surface over the local persistence records.

The route projects one workspace's evidence index, consent/audit trail,
and strategy lineage exactly as persisted by the local
persistence layer.  The router translates payloads only; the document source
is injected through app state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Path, Request

from thesis.http.errors import service_error

_STATE_DOCUMENT_KEYS = ("lineages", "evidence", "consentAudit")


class WorkspaceStateStore(Protocol):
    """Read/write boundary for one persisted workspace-state document."""

    def load(self, workspace_id: str) -> dict[str, object] | None:
        """Return the persisted state document, or ``None`` when absent."""
        ...

    def save(self, workspace_id: str, document: dict[str, object]) -> None:
        """Persist one workspace-state document."""
        ...


class InMemoryWorkspaceStateStore:
    """Local in-memory state store; the composition helper may persist it."""

    def __init__(self) -> None:
        self._documents: dict[str, dict[str, object]] = {}

    def load(self, workspace_id: str) -> dict[str, object] | None:
        document = self._documents.get(workspace_id)
        return dict(document) if document is not None else None

    def save(self, workspace_id: str, document: dict[str, object]) -> None:
        self._documents[workspace_id] = dict(document)


def _validate_document(document: dict[str, object]) -> dict[str, object]:
    for key in _STATE_DOCUMENT_KEYS:
        if key not in document:
            raise ValueError(f"workspace state document requires {key!r}")
    return {key: document[key] for key in _STATE_DOCUMENT_KEYS}


class WorkspaceStateService:
    """Serves one workspace's persisted lineage, evidence, and audit records."""

    def __init__(
        self,
        store: WorkspaceStateStore | None = None,
        *,
        clock: Callable[[], object] | None = None,
    ) -> None:
        self._store = store if store is not None else InMemoryWorkspaceStateStore()
        self._clock = clock

    @property
    def store(self) -> WorkspaceStateStore:
        return self._store

    def state(self, workspace_id: str) -> dict[str, object]:
        document = self._store.load(workspace_id)
        if document is None:
            raise KeyError(workspace_id)
        return _validate_document(document)

    def save_state(self, workspace_id: str, document: dict[str, object]) -> None:
        self._store.save(workspace_id, _validate_document(document))


def get_workspace_state_service(request: Request) -> WorkspaceStateService:
    """Resolve the service from app state so applications control persistence."""
    from thesis.http.errors import require_service

    return require_service(request, "workspace_state_service", WorkspaceStateService)


def create_workspace_state_router() -> APIRouter:
    """Create the workspace-state router; services resolve from app state."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["workspace-state"])

    @router.get("/state")
    def get_workspace_state(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[WorkspaceStateService, Depends(get_workspace_state_service)],
    ) -> dict[str, object]:
        try:
            return service.state(workspace_id)
        except KeyError as error:
            raise service_error(error, resource="workspace_state") from error
        except ValueError as error:
            raise service_error(error) from error

    @router.put("/state")
    def put_workspace_state(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        document: dict[str, object],
        service: Annotated[WorkspaceStateService, Depends(get_workspace_state_service)],
    ) -> dict[str, object]:
        try:
            service.save_state(workspace_id, document)
        except ValueError as error:
            raise service_error(error) from error
        return service.state(workspace_id)

    return router


__all__ = [
    "InMemoryWorkspaceStateStore",
    "WorkspaceStateService",
    "WorkspaceStateStore",
    "create_workspace_state_router",
    "get_workspace_state_service",
]
