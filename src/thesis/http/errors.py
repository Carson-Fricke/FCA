"""Shared HTTP error translation and fail-closed service resolution."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict

from thesis.chat import (
    CrossWorkspaceLinkError,
    InvalidLifecycleTransition,
    RecordNotFound,
    ToolJobConflictError,
)
from thesis.integrations.harness_activation import (
    CapabilityUnavailableError,
    HarnessActivationError,
)
from thesis.integrations.mcp import (
    HarnessMcpAuthError,
    HarnessMcpError,
    HarnessMcpProtocolError,
    HarnessMcpToolError,
    HarnessMcpTransportError,
)

_LOGGER = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: object | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail


def unavailable(name: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"code": f"{name}_unavailable", "message": f"{name} is not configured"},
    )


def require_service[Service](
    request: Request, name: str, expected: type[Service]
) -> Service:
    service = getattr(request.app.state, name, None)
    if service is None:
        raise unavailable(name)
    if not isinstance(service, expected):
        raise RuntimeError(f"app.state.{name} must be a {expected.__name__}")
    return service


def service_error(error: Exception, *, resource: str = "record") -> HTTPException:
    """Map declared exception types, never infer semantics from message text."""
    from thesis.http.deployments import SurfaceUnavailable
    from thesis.integrations.runtime_status import HarnessStatusError

    if isinstance(error, HTTPException):
        return error
    details: object | None = None
    if isinstance(error, RecordNotFound):
        status, code, message = 404, "record_not_found", str(error)
    elif isinstance(error, KeyError):
        status, code, message = (
            404,
            f"{resource}_not_found",
            f"{resource} was not found",
        )
    elif isinstance(error, CrossWorkspaceLinkError):
        status, code, message = 409, "cross_workspace_link", str(error)
    elif isinstance(error, ToolJobConflictError):
        status, code, message = 409, "tool_job_conflict", str(error)
    elif isinstance(error, InvalidLifecycleTransition):
        status, code, message = 409, "invalid_lifecycle_transition", str(error)
    elif isinstance(error, HarnessMcpAuthError):
        status, code, message = 503, "harness_mcp_auth_error", str(error)
    elif isinstance(error, HarnessMcpTransportError):
        status, code, message = 503, "harness_mcp_unreachable", str(error)
    elif isinstance(error, HarnessMcpToolError):
        status, code, message = 502, "harness_tool_error", str(error)
        details = error.payload
    elif isinstance(error, HarnessMcpProtocolError):
        status, code, message = 502, "harness_protocol_error", str(error)
    elif isinstance(error, HarnessMcpError):
        status, code, message = 502, "harness_mcp_error", str(error)
    elif isinstance(error, (CapabilityUnavailableError, HarnessActivationError)):
        status, code, message = 503, "harness_unavailable", str(error)
    elif isinstance(error, SurfaceUnavailable):
        status, code, message = 503, "surface_unavailable", str(error)
    elif isinstance(error, HarnessStatusError):
        status, code, message = 503, "harness_status_unavailable", str(error)
    elif isinstance(error, ValueError):
        status, code, message = 422, "validation_error", str(error)
    else:
        _LOGGER.error("HTTP service request failed", exc_info=error)
        status, code, message = 500, "internal_error", "request failed"
    if isinstance(error, (HarnessMcpError, HarnessActivationError)):
        retained_job_id = error.job_id
        if retained_job_id is not None:
            retained = {} if not isinstance(details, Mapping) else dict(details)
            retained["job_id"] = retained_job_id
            if isinstance(error, HarnessMcpError):
                retained["executionCertainty"] = error.execution_certainty
            details = retained
    detail: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        detail["details"] = details
    return HTTPException(status_code=status, detail=detail)
