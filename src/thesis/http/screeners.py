"""Screener HTTP surface delegating runs to the screening engine.

Definitions are stored in the workspace's local persistence with the
UI-declared filter vocabulary; a run translates the filters, delegates to
:func:`thesis.screening.engine.run_screener` over the injected market port,
and persists the study artifact through the screening artifacts module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from thesis.screening.artifacts import SavedStudy, save_study
from thesis.screening.engine import (
    ScreenerContext,
    ScreenerDefinition,
    ScreenerError,
    ScreenerPortError,
    ScreenerResult,
    run_screener,
)

_FILTER_PRESET_BY_KIND: dict[str, str] = {
    "volume": "avg_dollar_volume_min",
    "liquidity": "avg_dollar_volume_min",
    "price": "min_price",
}
_SUPPORTED_OPS = (">", ">=")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ScreenerMarketPort(ScreenerContext, Protocol):
    """Market ports plus the daily change metric for result rows."""

    def change_pct(self, symbol: str, as_of: date) -> float:
        """Return the day change percentage for one symbol."""
        ...


class ScreenerStore(Protocol):
    """Read/write boundary for screener definitions and study artifacts."""

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        """Return every persisted screener definition."""
        ...

    def add(self, workspace_id: str, row: dict[str, object]) -> None:
        """Persist one screener definition row."""
        ...

    def save_artifact(self, path: str, payload: bytes) -> None:
        """Persist one study artifact under its workspace-relative path."""
        ...


class InMemoryScreenerStore:
    """Local in-memory screener definition and artifact store."""

    def __init__(self) -> None:
        self._definitions: dict[str, list[dict[str, object]]] = {}
        self._artifacts: dict[str, bytes] = {}

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return tuple(dict(row) for row in self._definitions.get(workspace_id, ()))

    def add(self, workspace_id: str, row: dict[str, object]) -> None:
        self._definitions.setdefault(workspace_id, []).insert(0, dict(row))

    def save_artifact(self, path: str, payload: bytes) -> None:
        self._artifacts[path] = payload

    def load_artifact(self, path: str) -> bytes | None:
        return self._artifacts.get(path)


class RunScreenerRequest(BaseModel):
    """Request boundary carrying the UI filter vocabulary."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    filters: tuple[dict[str, object], ...] = ()
    universe_revision: str = Field(default="local", min_length=1, max_length=128)
    lookback_days: int = Field(default=20, ge=1, le=750)


def _translate_filters(
    filters: tuple[dict[str, object], ...],
) -> tuple[Mapping[str, object], ...]:
    """Translate the UI filter vocabulary into engine filter specs."""
    if not filters:
        raise ValueError("at least one filter is required")
    specs: list[Mapping[str, object]] = []
    for spec in filters:
        kind = spec.get("kind")
        op = spec.get("op")
        value = spec.get("value")
        if not isinstance(kind, str) or kind not in _FILTER_PRESET_BY_KIND:
            raise ValueError(f"unsupported screener filter kind {kind!r}")
        if op not in _SUPPORTED_OPS:
            raise ValueError(f"unsupported screener filter operator {op!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("screener filter value must be a number")
        specs.append({"kind": _FILTER_PRESET_BY_KIND[kind], "params": {"min": value}})
    return tuple(specs)


@dataclass(frozen=True, slots=True)
class ScreenerDataService:
    """Delegates runs to the screening engine over the injected market port."""

    store: ScreenerStore
    market: ScreenerMarketPort
    clock: Callable[[], datetime]
    id_factory: Callable[[str], str] | None = None
    strategy_matcher: Callable[[str], str | None] | None = None
    git_revision: str = "local"

    def list_screeners(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return self.store.list(workspace_id)

    def run(self, workspace_id: str, request: RunScreenerRequest) -> dict[str, object]:
        """Run one screen, persist the study artifact, and record the row."""
        try:
            engine_filters = _translate_filters(request.filters)
            definition = ScreenerDefinition(
                name=request.name,
                universe_revision=request.universe_revision,
                filters=engine_filters,
                lookback_days=request.lookback_days,
            )
            as_of = self.clock().astimezone(UTC).date()
            result = run_screener(definition, self.market, as_of=as_of)
        except ScreenerPortError as error:
            raise ScreenerPortUnavailable(str(error)) from error
        except ScreenerError as error:
            raise ValueError(str(error)) from error

        study = SavedStudy(
            name="-".join(part for part in request.name.lower().split() if part),
            definition=definition,
            result=result,
            git_revision=self.git_revision,
            saved_at=as_of,
        )
        artifact_path = study.artifact_path()
        self.store.save_artifact(artifact_path, save_study(study))
        row: dict[str, object] = {
            "id": (
                self.id_factory("scr") if self.id_factory is not None else study.name
            ),
            "workspace_id": workspace_id,
            "name": request.name,
            "filters": [dict(item) for item in request.filters],
            "created_at": _iso_z(self.clock()),
            "artifact_path": artifact_path,
        }
        self.store.add(workspace_id, row)
        return {
            "items": self._result_rows(result),
            "artifact_path": artifact_path,
            "definition_hash": result.definition_hash,
            "as_of": result.as_of.isoformat(),
        }

    def _result_rows(self, result: ScreenerResult) -> list[dict[str, object]]:
        matcher = self.strategy_matcher
        rows: list[dict[str, object]] = []
        for hit in result.hits:
            metrics = hit.metrics
            rows.append(
                {
                    "symbol": hit.symbol,
                    "rank": hit.rank,
                    "last": float(metrics.get("last_close", "0")),
                    "volume": float(metrics.get("avg_volume", "0")),
                    "avg_dollar_volume_20d": float(
                        metrics.get("avg_dollar_volume", "0")
                    ),
                    "change_pct": self.market.change_pct(hit.symbol, result.as_of),
                    "matched_strategy_id": (
                        matcher(hit.symbol) if matcher is not None else None
                    ),
                }
            )
        return rows


class ScreenerPortUnavailable(RuntimeError):
    """The injected market ports failed; the screen result is unavailable."""


def get_screener_service(request: Request) -> ScreenerDataService:
    """Resolve the service from app state so applications control persistence."""
    service = getattr(request.app.state, "screener_data_service", None)
    if service is None:
        raise RuntimeError(
            "app.state.screener_data_service must be configured with a market port"
        )
    if not isinstance(service, ScreenerDataService):
        raise RuntimeError(
            "app.state.screener_data_service must be a ScreenerDataService"
        )
    return service


def create_screeners_router() -> APIRouter:
    """Create the screener router; services resolve from app state."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["screeners"])

    @router.get("/screeners")
    async def list_screener_definitions(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[ScreenerDataService, Depends(get_screener_service)],
    ) -> dict[str, object]:
        return {"items": list(service.list_screeners(workspace_id))}

    @router.post("/screeners/run")
    async def run_screener_definition(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: RunScreenerRequest,
        service: Annotated[ScreenerDataService, Depends(get_screener_service)],
    ) -> dict[str, object]:
        try:
            return service.run(workspace_id, request)
        except ScreenerPortUnavailable as error:
            raise HTTPException(
                status_code=503,
                detail={"code": "screener_port_unavailable", "message": str(error)},
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    return router


__all__ = [
    "InMemoryScreenerStore",
    "RunScreenerRequest",
    "ScreenerDataService",
    "ScreenerMarketPort",
    "ScreenerStore",
    "create_screeners_router",
    "get_screener_service",
]
