"""Workspace strategy registry HTTP surface.

Strategy summaries are workspace records served to the Thesis UI registry
page. The router only translates HTTP requests into service calls and resolves
the service from app state, like the other workspace data routers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from thesis.strategies import WorkspaceStrategyArtifacts


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


class CreateStrategyRequest(BaseModel):
    """Request boundary for a new strategy registry entry."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    symbol: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=1_024)
    status: str = Field(default="draft", pattern="^(live|paper|paused|draft)$")


class StrategyDataService:
    """Read/write surface over the workspace strategy store."""

    def __init__(
        self,
        store: StrategyStore | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
        artifacts: WorkspaceStrategyArtifacts | None = None,
    ) -> None:
        self._store = store if store is not None else InMemoryStrategyStore()
        self._clock = clock if clock is not None else _utc_now
        self._id_factory = id_factory
        self.artifacts = artifacts

    @property
    def store(self) -> StrategyStore:
        return self._store

    def _next_id(self) -> str:
        if self._id_factory is None:
            stamp = _iso_z(self._clock()).replace("-", "").replace(":", "")
            return f"str-{stamp}"
        return self._id_factory("str")

    def list_strategies(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        if self.artifacts is not None:
            return self.artifacts.list(workspace_id)
        return self._store.list(workspace_id)

    def add_strategy(
        self, workspace_id: str, strategy: dict[str, object]
    ) -> dict[str, object]:
        if self.artifacts is not None:
            raise ValueError("strategies must be authored as workspace Rust artifacts")
        return self._store.add(workspace_id, strategy)

    def create_strategy(
        self,
        workspace_id: str,
        *,
        name: str,
        symbol: str,
        description: str = "",
        status: str = "draft",
    ) -> dict[str, object]:
        """Create a draft strategy registry entry with an initial version."""
        if self.artifacts is not None:
            raise ValueError("use author_strategy to publish a Rust workspace artifact")
        now = _iso_z(self._clock())
        strategy: dict[str, object] = {
            "id": self._next_id(),
            "workspace_id": workspace_id,
            "name": name,
            "symbol": symbol,
            "status": status,
            "description": description,
            "pnl": 0.0,
            "return_pct": 0.0,
            "win_rate": 0.0,
            "capital": 0.0,
            "code": "",
            "versions": [
                {
                    "version": "0.1.0",
                    "created_at": now,
                    "parent_version": None,
                    "note": "Created",
                }
            ],
            "created_at": now,
        }
        return self._store.add(workspace_id, strategy)


def get_strategy_service(request: Request) -> StrategyDataService:
    """Resolve the service from app state so applications control persistence."""
    from thesis.http.errors import require_service

    return require_service(request, "strategy_service", StrategyDataService)


def create_strategy_router() -> APIRouter:
    """Create the workspace strategy router; services resolve from app state."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["strategies"])

    @router.get("/strategies")
    async def list_strategies(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[StrategyDataService, Depends(get_strategy_service)],
    ) -> dict[str, object]:
        return {"items": list(service.list_strategies(workspace_id))}

    @router.post("/strategies", status_code=201)
    async def create_strategy(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: CreateStrategyRequest,
        service: Annotated[StrategyDataService, Depends(get_strategy_service)],
    ) -> dict[str, object]:
        from thesis.http.errors import service_error

        try:
            return service.create_strategy(
                workspace_id,
                name=request.name,
                symbol=request.symbol,
                description=request.description,
                status=request.status,
            )
        except ValueError as error:
            raise service_error(error) from error

    return router


__all__ = [
    "CreateStrategyRequest",
    "InMemoryStrategyStore",
    "StrategyDataService",
    "StrategyStore",
    "create_strategy_router",
    "get_strategy_service",
]
