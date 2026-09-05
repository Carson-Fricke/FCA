"""Minimal FastAPI application for the quant-harness HTTP boundary."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from harness.http.launcher import BacktestJobLauncher, InProcessBacktestLauncher
from harness.http.routes.launch import create_launch_router
from harness.http.routes.runs import create_runs_router
from harness.results.store import (
    BacktestResultsStore,
    InMemoryRunBlobStore,
    InMemoryRunCatalogRepository,
)
from lake.catalog.memory import InMemoryJobRepository
from lake.catalog.ports import JobRepository
from lake.http.auth import CombinedAuthenticator, EntraJwtAuthenticator
from lake.http.models import AuthCheckResponse, ErrorResponse, HealthResponse
from lake.storage.ports import SasIssuer


def configured_bearer_token(bearer_token: str | None) -> str | None:
    """Return the explicit token or the runtime-configured static token."""
    token = (
        os.environ.get("HARNESS_API__BEARER_TOKEN")
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(
    *,
    bearer_token: str | None = None,
    results_store: BacktestResultsStore | None = None,
    job_repository: JobRepository | None = None,
    job_launcher: BacktestJobLauncher | None = None,
    sas_issuer: SasIssuer | None = None,
    data_source: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    entra_tenant_id: str | None = None,
    entra_audience: str | None = None,
    entra_role: str = "Harness.Access",
) -> FastAPI:
    """Build an API app with an injected, testable authentication dependency."""
    entra_authenticator = (
        EntraJwtAuthenticator(
            tenant_id=entra_tenant_id,
            audience=entra_audience,
            role=entra_role,
        )
        if entra_tenant_id is not None and entra_audience is not None
        else None
    )
    authenticator = CombinedAuthenticator(
        configured_bearer_token(bearer_token), entra=entra_authenticator
    )
    application = FastAPI(
        title="Quant Harness API",
        version="0.1.0",
        responses={
            status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
    )
    resolved_store = results_store
    if resolved_store is None:
        resolved_store = BacktestResultsStore(
            InMemoryRunBlobStore(),
            InMemoryRunCatalogRepository(),
        )
    resolved_jobs = (
        InMemoryJobRepository() if job_repository is None else job_repository
    )
    resolved_clock: Callable[[], datetime] = _utc_now if clock is None else clock
    resolved_launcher: BacktestJobLauncher = (
        InProcessBacktestLauncher(
            resolved_store,
            resolved_jobs,
            data_source=data_source,
            clock=resolved_clock,
        )
        if job_launcher is None
        else job_launcher
    )
    application.state.results_store = resolved_store
    application.state.job_repository = resolved_jobs
    application.state.job_launcher = resolved_launcher
    application.state.sas_issuer = sas_issuer
    application.state.clock = resolved_clock

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
        """Provide a minimal protected route for credential checks."""
        return AuthCheckResponse(status="ok")

    application.include_router(
        create_runs_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_launch_router(),
        dependencies=[Depends(authenticator)],
    )

    return application


app = create_app()


__all__ = ["app", "configured_bearer_token", "create_app"]
