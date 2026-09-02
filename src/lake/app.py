"""Minimal FastAPI application for the market-lake HTTP boundary."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from lake.auth import BearerAuthenticator
from lake.bounded_query import BoundedDuckDBQuery, QueryLimits
from lake.catalog import (
    CatalogRepository,
    InMemoryCatalogRepository,
    InMemoryJobRepository,
    JobRepository,
)
from lake.catalog_api import create_catalog_router
from lake.ensure_api import EnsureJobLauncher, create_ensure_router
from lake.models import AuthCheckResponse, ErrorResponse, HealthResponse
from lake.query_api import EnsureCallback, create_query_router
from lake.settings import load_settings
from lake.storage import SasIssuer


def _configured_token(bearer_token: str | None) -> str | None:
    token = (
        os.environ.get("LAKE_API__BEARER_TOKEN")
        if bearer_token is None
        else bearer_token
    )
    if token is not None and not token.strip():
        raise ValueError("bearer token must be a non-empty string")
    return token


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: Mapping[str, str] | None = None,
    details: object | None = None,
) -> JSONResponse:
    error: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        error["details"] = jsonable_encoder(details)
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers=headers,
    )


def create_app(
    *,
    bearer_token: str | None = None,
    catalog_repository: CatalogRepository | None = None,
    job_repository: JobRepository | None = None,
    job_launcher: EnsureJobLauncher | None = None,
    catalog_max_range_days: int | None = None,
    ensure_wait_deadline_seconds: int | float | None = None,
    coverage_clock: Callable[[], datetime] | None = None,
    sas_issuer: SasIssuer | None = None,
    ensure_callback: EnsureCallback | None = None,
    bounded_query: BoundedDuckDBQuery | None = None,
    query_file_root: str | os.PathLike[str] | None = None,
    sql_max_scan_bytes: int | None = None,
    sql_max_result_bytes: int | None = None,
    authorized_force_override: bool | None = None,
) -> FastAPI:
    """Build an API app with an injected, testable authentication dependency."""
    authenticator = BearerAuthenticator(_configured_token(bearer_token))
    settings = load_settings()
    application = FastAPI(
        title="Market Lake API",
        version="0.1.0",
        responses={
            status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
    )
    application.state.catalog_repository = (
        InMemoryCatalogRepository()
        if catalog_repository is None
        else catalog_repository
    )
    application.state.job_repository = (
        InMemoryJobRepository() if job_repository is None else job_repository
    )
    application.state.job_launcher = job_launcher
    application.state.sas_issuer = sas_issuer
    application.state.ensure_callback = ensure_callback
    if bounded_query is None and query_file_root is not None:
        bounded_query = BoundedDuckDBQuery(
            Path(query_file_root),
            QueryLimits(
                max_scan_bytes=(
                    settings.api.sql.max_scan_bytes
                    if sql_max_scan_bytes is None
                    else sql_max_scan_bytes
                ),
                max_result_bytes=(
                    settings.api.sql.max_result_bytes
                    if sql_max_result_bytes is None
                    else sql_max_result_bytes
                ),
            ),
        )
    application.state.bounded_query = bounded_query
    application.state.authorized_force_override = (
        settings.api.sql.authorized_force_override_default
        if authorized_force_override is None
        else authorized_force_override
    )
    application.state.catalog_max_range_days = (
        settings.api.catalog.max_range_days
        if catalog_max_range_days is None
        else catalog_max_range_days
    )
    application.state.ensure_wait_deadline_seconds = (
        settings.api.ensure_wait_deadline_seconds
        if ensure_wait_deadline_seconds is None
        else ensure_wait_deadline_seconds
    )
    if coverage_clock is not None:
        application.state.coverage_clock = coverage_clock

    @application.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exception: StarletteHTTPException
    ) -> JSONResponse:
        del request
        detail = exception.detail
        if isinstance(detail, dict):
            code = detail.get("code")
            message = detail.get("message")
            if isinstance(code, str) and isinstance(message, str):
                return _error_response(
                    exception.status_code,
                    code,
                    message,
                    headers=exception.headers,
                )
        return _error_response(
            exception.status_code,
            "http_error",
            str(detail),
            headers=exception.headers,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exception: RequestValidationError
    ) -> JSONResponse:
        del request
        return _error_response(
            422,
            "validation_error",
            "request validation failed",
            details=exception.errors(),
        )

    @application.get(
        "/health",
        response_model=HealthResponse,
        tags=["health"],
    )
    @application.get(
        "/healthz",
        response_model=HealthResponse,
        include_in_schema=False,
    )
    async def health() -> HealthResponse:
        """Return a probe-safe liveness response without requiring credentials."""
        return HealthResponse(status="ok")

    @application.get(
        "/v1/auth/check",
        response_model=AuthCheckResponse,
        dependencies=[Depends(authenticator)],
        tags=["auth"],
    )
    async def auth_check() -> AuthCheckResponse:
        """Provide a minimal protected route until feature routes are implemented."""
        return AuthCheckResponse(status="ok")

    application.include_router(
        create_catalog_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_ensure_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_query_router(),
        dependencies=[Depends(authenticator)],
    )

    return application


app = create_app()


__all__ = ["app", "create_app"]
