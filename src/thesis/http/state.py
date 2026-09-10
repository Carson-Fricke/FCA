"""Workspace-state HTTP surface over the local persistence records.

The route projects one workspace's CLEO projection view, evidence index,
consent/audit trail, and strategy lineage exactly as persisted by the local
persistence layer.  The router translates payloads only; the document source
is injected through app state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Request

_STATE_DOCUMENT_KEYS = ("cleo", "taskTimeline", "lineages", "evidence", "consentAudit")


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
    return document


class WorkspaceStateService:
    """Serves one workspace's persisted CLEO projection and audit records."""

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
        return document

    def save_state(self, workspace_id: str, document: dict[str, object]) -> None:
        _validate_document(document)
        self._store.save(workspace_id, document)


def get_workspace_state_service(request: Request) -> WorkspaceStateService:
    """Resolve the service from app state so applications control persistence."""
    service = getattr(request.app.state, "workspace_state_service", None)
    if service is None:
        service = WorkspaceStateService()
        request.app.state.workspace_state_service = service
    if not isinstance(service, WorkspaceStateService):
        raise RuntimeError(
            "app.state.workspace_state_service must be a WorkspaceStateService"
        )
    return service


def create_workspace_state_router() -> APIRouter:
    """Create the workspace-state router; services resolve from app state."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["workspace-state"])

    @router.get("/state")
    async def get_workspace_state(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[WorkspaceStateService, Depends(get_workspace_state_service)],
    ) -> dict[str, object]:
        try:
            return service.state(workspace_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "workspace_state_not_found",
                    "message": f"no persisted state for workspace {workspace_id!r}",
                },
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.put("/state")
    async def put_workspace_state(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        document: dict[str, object],
        service: Annotated[WorkspaceStateService, Depends(get_workspace_state_service)],
    ) -> dict[str, object]:
        try:
            service.save_state(workspace_id, document)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return service.state(workspace_id)

    return router


__all__ = [
    "InMemoryWorkspaceStateStore",
    "WorkspaceStateService",
    "WorkspaceStateStore",
    "create_workspace_state_router",
    "get_workspace_state_service",
]
