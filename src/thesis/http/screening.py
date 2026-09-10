"""Thin HTTP adapter for the workspace screening surface.

The router only translates validated HTTP payloads into service calls on
:class:`~thesis.screening.service.ScreenerService`; it has no knowledge of
the lake, the harness indicator registry, or engine internals.  Applications
inject the service (with its data-context factory) through app state, and
run responses carry the full deterministic provenance (universe revision,
as-of date, data window, definition hash, content result id).

The router serves the engine-native declarative contract under
``/v1/workspaces/{workspace_id}/screening`` — the legacy UI filter
vocabulary is served separately by the screeners router.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from thesis.screening.engine import ScreenerError
from thesis.screening.service import (
    ScreenerDataUnavailableError,
    ScreenerService,
    ScreenerServiceError,
    UnknownScreenerError,
)


class ScreenerHTTPModel(BaseModel):
    """Strict request and response boundary for the screener client."""

    model_config = ConfigDict(extra="forbid")


class ScreenerFilterSpec(ScreenerHTTPModel):
    """One declarative filter: a preset kind with its parameters."""

    kind: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class ScreenerDefinitionRequest(ScreenerHTTPModel):
    """A saved screener definition payload."""

    name: str = Field(min_length=1, max_length=256)
    universe_revision: str | None = Field(default=None, max_length=128)
    lookback_days: int = Field(default=20, ge=1, le=365)
    filters: tuple[ScreenerFilterSpec, ...] = Field(min_length=1)


class RunScreenerRequest(ScreenerHTTPModel):
    """One screening run at an explicit point-in-time as-of date."""

    name: str = Field(min_length=1, max_length=256)
    as_of: date
    universe_revision: str | None = Field(default=None, max_length=128)
    lookback_days: int = Field(default=20, ge=1, le=365)
    filters: tuple[ScreenerFilterSpec, ...] = Field(min_length=1)


def get_screener_service(request: Request) -> ScreenerService:
    """Resolve the service from app state so applications control data wiring."""
    service = getattr(request.app.state, "screener_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "screener_unavailable",
                "message": "screener data source is not configured",
            },
        )
    return service


def _service_error(error: Exception) -> HTTPException:
    if isinstance(error, UnknownScreenerError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ScreenerDataUnavailableError):
        return HTTPException(
            status_code=503,
            detail={"code": "screener_unavailable", "message": str(error)},
        )
    if isinstance(error, (ScreenerServiceError, ScreenerError)):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=500, detail="screener request failed")


def _payload(request: ScreenerDefinitionRequest | RunScreenerRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": request.name,
        "universe_revision": request.universe_revision,
        "lookback_days": request.lookback_days,
        "filters": [
            {"kind": spec.kind, "params": dict(spec.params)} for spec in request.filters
        ],
    }
    # The service derives a default universe revision from the run's as-of
    # instant; saved definitions keep the caller-declared revision only.
    if isinstance(request, RunScreenerRequest):
        payload["as_of"] = request.as_of.isoformat()
    return payload


def create_screener_router() -> APIRouter:
    """Create the workspace-scoped screener router."""
    router = APIRouter(
        prefix="/v1/workspaces/{workspace_id}/screening", tags=["screening"]
    )

    @router.post("/run")
    async def run_screener_route(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: RunScreenerRequest,
        service: Annotated[ScreenerService, Depends(get_screener_service)],
    ) -> dict[str, object]:
        """Run one definition against the workspace's point-in-time data."""
        try:
            return dict(
                service.run(workspace_id, _payload(request), as_of=request.as_of)
            )
        except Exception as error:
            raise _service_error(error) from error

    @router.post("")
    async def save_screener(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: ScreenerDefinitionRequest,
        service: Annotated[ScreenerService, Depends(get_screener_service)],
    ) -> Mapping[str, object]:
        """Save one screener definition as a workspace artifact document."""
        try:
            return dict(service.save_definition(workspace_id, _payload(request)))
        except Exception as error:
            raise _service_error(error) from error

    @router.get("")
    async def list_screeners(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[ScreenerService, Depends(get_screener_service)],
    ) -> dict[str, object]:
        """List the workspace's saved definitions in deterministic order."""
        return {"items": [dict(doc) for doc in service.list_definitions(workspace_id)]}

    @router.get("/results/{result_id}")
    async def get_screener_result(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        result_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[ScreenerService, Depends(get_screener_service)],
    ) -> Mapping[str, object]:
        """Return one stored run result with its full provenance."""
        try:
            return dict(service.get_result(workspace_id, result_id))
        except Exception as error:
            raise _service_error(error) from error

    @router.get("/{screener_id}")
    async def get_screener(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        screener_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[ScreenerService, Depends(get_screener_service)],
    ) -> Mapping[str, object]:
        """Return one saved definition document."""
        try:
            return dict(service.get_definition(workspace_id, screener_id))
        except Exception as error:
            raise _service_error(error) from error

    return router


__all__ = [
    "RunScreenerRequest",
    "ScreenerDefinitionRequest",
    "ScreenerFilterSpec",
    "create_screener_router",
    "get_screener_service",
]
