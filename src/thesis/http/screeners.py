"""Workspace screener HTTP surface for the thin harness client.

Definitions are content-addressed workspace artifacts and runs execute on the
harness through the MCP boundary; the router only translates HTTP requests
into service calls and resolves the service from app state, like the other
workspace data routers.  With no harness MCP boundary composed the run route
fails closed with 503, unknown definition/result ids are 404, and invalid
definitions or run requests are 422.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from thesis.screening.definitions import ScreenerDefinition, ScreenerDefinitionError
from thesis.screening.runs import ScreeningRunError
from thesis.screening.service import ScreenerService, ScreenerUnavailableError


class SaveScreenerRequest(BaseModel):
    """Request boundary for one authored screener definition."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    lookback_days: int = Field(ge=1, le=3_650)
    bar_interval: str = Field(default="1d", pattern="^(1min|1d)$")
    rank_metric: str = Field(min_length=1, max_length=256)
    universe_revision: str | None = Field(default=None, min_length=1, max_length=256)
    source: str = Field(min_length=1)
    parameters: dict[str, object] | None = None


class RunScreenerRequest(BaseModel):
    """Request boundary for one screening run of a saved definition."""

    model_config = ConfigDict(extra="forbid")

    definition_id: str = Field(min_length=1, max_length=256)
    as_of: str = Field(min_length=1, max_length=64)


def get_screener_service(request: Request) -> ScreenerService:
    """Resolve the service from app state so applications control the boundary."""
    from thesis.http.errors import require_service

    return require_service(request, "screener_service", ScreenerService)


def _validation_error(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": "invalid_screener_definition", "message": str(error)},
    )


def _not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message})


def create_screener_router() -> APIRouter:
    """Create the screener router; services resolve from app state."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["screeners"])

    @router.get("/screeners")
    async def list_screeners(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[ScreenerService, Depends(get_screener_service)],
    ) -> dict[str, object]:
        return {
            "items": [
                definition.document()
                for definition in service.list_definitions(workspace_id)
            ]
        }

    @router.post("/screeners", status_code=201)
    async def save_screener(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: SaveScreenerRequest,
        service: Annotated[ScreenerService, Depends(get_screener_service)],
    ) -> dict[str, object]:
        try:
            definition = ScreenerDefinition(
                name=request.name,
                lookback_days=request.lookback_days,
                bar_interval=request.bar_interval,
                rank_metric=request.rank_metric,
                universe_revision=request.universe_revision,
                source=request.source,
                parameters=request.parameters,
            )
        except ScreenerDefinitionError as error:
            raise _validation_error(error) from error
        saved = service.save_definition(workspace_id, definition)
        return saved.document()

    @router.post("/screeners/run")
    async def run_screener(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: RunScreenerRequest,
        service: Annotated[ScreenerService, Depends(get_screener_service)],
    ) -> dict[str, object]:
        try:
            record = service.run(
                workspace_id, request.definition_id, as_of=request.as_of
            )
        except KeyError as error:
            definition_id = error.args[0]
            raise _not_found(
                "screener_not_found",
                f"screener definition {definition_id!r} does not exist",
            ) from error
        except ScreenerUnavailableError as error:
            detail: dict[str, object] = {
                "code": "harness_unavailable",
                "message": str(error),
            }
            if error.job_id is not None:
                detail["job_id"] = error.job_id
            raise HTTPException(status_code=503, detail=detail) from error
        except ScreeningRunError as error:
            detail = {
                "code": "screener_run_failed",
                "message": str(error),
            }
            if error.job_id is not None:
                detail["job_id"] = error.job_id
            raise HTTPException(status_code=502, detail=detail) from error
        except ScreenerDefinitionError as error:
            raise _validation_error(error) from error
        return record.document()

    @router.get("/screeners/results/{result_id}")
    async def get_screener_result(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        result_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[ScreenerService, Depends(get_screener_service)],
    ) -> dict[str, object]:
        try:
            record = service.get_result(workspace_id, result_id)
        except KeyError as error:
            raise _not_found(
                "screener_result_not_found",
                f"screening result {error.args[0]!r} does not exist",
            ) from error
        return record.document()

    return router


__all__ = [
    "RunScreenerRequest",
    "SaveScreenerRequest",
    "create_screener_router",
    "get_screener_service",
]
