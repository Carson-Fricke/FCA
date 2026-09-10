"""Workspace-persisted dashboard widget HTTP surface.

Widget definitions are workspace records backed by the local persistence
store; the router only translates validated HTTP payloads into service calls
and resolves the service from app state like the chat router does.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

_WIDGET_TYPES = ("equity", "pnl", "allocation", "winrate", "orders")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class WidgetStore(Protocol):
    """Read/write boundary for workspace-persisted widget definitions."""

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        """Return every persisted widget definition for the workspace."""
        ...

    def add(self, workspace_id: str, widget: dict[str, object]) -> dict[str, object]:
        """Persist and return one widget definition."""
        ...

    def delete(self, workspace_id: str, widget_id: str) -> None:
        """Remove one widget definition; raise ``KeyError`` when absent."""
        ...


class InMemoryWidgetStore:
    """Local in-memory widget store."""

    def __init__(self) -> None:
        self._widgets: dict[str, list[dict[str, object]]] = {}

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return tuple(dict(widget) for widget in self._widgets.get(workspace_id, ()))

    def add(self, workspace_id: str, widget: dict[str, object]) -> dict[str, object]:
        widgets = self._widgets.setdefault(workspace_id, [])
        stored = dict(widget)
        widgets.append(stored)
        return dict(stored)

    def delete(self, workspace_id: str, widget_id: str) -> None:
        widgets = self._widgets.get(workspace_id, [])
        for index, widget in enumerate(widgets):
            if widget.get("id") == widget_id:
                del widgets[index]
                return
        raise KeyError(widget_id)


class CreateWidgetRequest(BaseModel):
    """Request boundary for widget creation; shapes follow WidgetDefinition."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    type: str = Field(pattern="^(equity|pnl|allocation|winrate|orders)$")
    pinned: bool = False
    data: dict[str, object] = Field(default_factory=dict)


class DashboardDataService:
    """CRUD surface over the workspace widget store."""

    def __init__(
        self,
        store: WidgetStore | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._store = store if store is not None else InMemoryWidgetStore()
        self._clock = clock if clock is not None else _utc_now
        self._id_factory = id_factory

    @property
    def store(self) -> WidgetStore:
        return self._store

    def _next_id(self) -> str:
        if self._id_factory is None:
            return f"wgt-{_iso_z(self._clock()).replace('-', '').replace(':', '')}"
        return self._id_factory("wgt")

    def list_widgets(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return self._store.list(workspace_id)

    def create_widget(
        self,
        workspace_id: str,
        title: str,
        widget_type: str,
        pinned: bool,
        data: dict[str, object],
    ) -> dict[str, object]:
        widget: dict[str, object] = {
            "id": self._next_id(),
            "workspace_id": workspace_id,
            "title": title,
            "type": widget_type,
            "pinned": pinned,
            "created_at": _iso_z(self._clock()),
            "data": dict(data),
        }
        return self._store.add(workspace_id, widget)

    def delete_widget(self, workspace_id: str, widget_id: str) -> None:
        self._store.delete(workspace_id, widget_id)


def get_dashboard_service(request: Request) -> DashboardDataService:
    """Resolve the service from app state so applications control persistence."""
    service = getattr(request.app.state, "dashboard_service", None)
    if service is None:
        service = DashboardDataService()
        request.app.state.dashboard_service = service
    if not isinstance(service, DashboardDataService):
        raise RuntimeError("app.state.dashboard_service must be a DashboardDataService")
    return service


def create_dashboard_router() -> APIRouter:
    """Create the dashboard widget router; services resolve from app state."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["dashboards"])

    @router.get("/dashboards/widgets")
    async def list_widgets(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[DashboardDataService, Depends(get_dashboard_service)],
    ) -> dict[str, object]:
        return {"items": list(service.list_widgets(workspace_id))}

    @router.post("/dashboards/widgets", status_code=201)
    async def create_widget(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: CreateWidgetRequest,
        service: Annotated[DashboardDataService, Depends(get_dashboard_service)],
    ) -> dict[str, object]:
        return service.create_widget(
            workspace_id,
            title=request.title,
            widget_type=request.type,
            pinned=request.pinned,
            data=request.data,
        )

    @router.delete("/dashboards/widgets/{widget_id}", status_code=204)
    async def delete_widget(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        widget_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[DashboardDataService, Depends(get_dashboard_service)],
    ) -> None:
        try:
            service.delete_widget(workspace_id, widget_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "widget_not_found",
                    "message": f"widget {widget_id!r} does not exist",
                },
            ) from error

    return router


__all__ = [
    "CreateWidgetRequest",
    "DashboardDataService",
    "InMemoryWidgetStore",
    "WidgetStore",
    "create_dashboard_router",
    "get_dashboard_service",
]
