"""Workspace strategy registry HTTP surface.

Strategy summaries are workspace records served to the Thesis UI registry
page. The router only translates HTTP requests into service calls and resolves
the service from app state, like the other workspace data routers.
"""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Path, Request


class StrategyStore(Protocol):
    """Read/write boundary for workspace-persisted strategy summaries."""

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        """Return every strategy summary for the workspace."""
        ...

    def add(self, workspace_id: str, strategy: dict[str, object]) -> dict[str, object]:
        """Persist and return one strategy summary."""
        ...


class InMemoryStrategyStore:
    """Local in-memory strategy store."""

    def __init__(self) -> None:
        self._strategies: dict[str, list[dict[str, object]]] = {}

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return tuple(
            dict(strategy) for strategy in self._strategies.get(workspace_id, ())
        )

    def add(self, workspace_id: str, strategy: dict[str, object]) -> dict[str, object]:
        strategies = self._strategies.setdefault(workspace_id, [])
        stored = dict(strategy)
        strategies.append(stored)
        return dict(stored)


class StrategyDataService:
    """Read surface over the workspace strategy store."""

    def __init__(self, store: StrategyStore | None = None) -> None:
        self._store = store if store is not None else InMemoryStrategyStore()

    @property
    def store(self) -> StrategyStore:
        return self._store

    def list_strategies(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return self._store.list(workspace_id)

    def add_strategy(
        self, workspace_id: str, strategy: dict[str, object]
    ) -> dict[str, object]:
        return self._store.add(workspace_id, strategy)


def get_strategy_service(request: Request) -> StrategyDataService:
    """Resolve the service from app state so applications control persistence."""
    service = getattr(request.app.state, "strategy_service", None)
    if service is None:
        service = StrategyDataService()
        request.app.state.strategy_service = service
    if not isinstance(service, StrategyDataService):
        raise RuntimeError("app.state.strategy_service must be a StrategyDataService")
    return service


def create_strategy_router() -> APIRouter:
    """Create the workspace strategy router; services resolve from app state."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["strategies"])

    @router.get("/strategies")
    async def list_strategies(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[StrategyDataService, Depends(get_strategy_service)],
    ) -> dict[str, object]:
        return {"items": list(service.list_strategies(workspace_id))}

    return router


__all__ = [
    "InMemoryStrategyStore",
    "StrategyDataService",
    "StrategyStore",
    "create_strategy_router",
    "get_strategy_service",
]
