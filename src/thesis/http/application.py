"""Composed Thesis HTTP application.

The app composes the UI-facing surface described by the chat/execution
boundary: authentication, the chat router (the sole messaging surface), and
the SSE streaming router (both surfaces of an execution).  Authentication
mirrors the market-lake ``CombinedAuthenticator`` pattern: a single static
bearer token, optional Entra JWT validation, a uniform 401 error shape, and
fail-closed behavior when no token is configured.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from secrets import compare_digest
from typing import TYPE_CHECKING, Annotated

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWK, PyJWKClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from thesis.agent.harness_tools import HarnessToolProvider, HarnessToolResult
from thesis.agent.reply import reply_provider_for
from thesis.chat import ChatService
from thesis.chat.responder import ChatReplyService, ToolResultChunk, ToolResultObserver
from thesis.chat.streaming import InMemoryChatStreamBroker
from thesis.http.backtests import create_backtest_router
from thesis.http.chat import create_chat_router
from thesis.http.dashboards import create_dashboard_router
from thesis.http.deployments import create_deployment_router
from thesis.http.harness import HarnessDataService, create_harness_router
from thesis.http.knowledge import create_knowledge_router
from thesis.http.screeners import create_screeners_router
from thesis.http.screening import create_screener_router
from thesis.http.state import create_workspace_state_router
from thesis.http.strategies import create_strategy_router
from thesis.http.streaming import (
    ProviderDeltaSource,
    StreamSleeper,
    create_streaming_router,
)
from thesis.http.sub_agents import create_sub_agent_router
from thesis.http.validation import create_validation_router
from thesis.http.workspaces import (
    WorkspaceDataBundle,
    create_workspaces_router,
)
from thesis.providers.openrouter import OpenRouterProvider
from thesis.screening.service import ScreenerService

if TYPE_CHECKING:
    from thesis.backtests import BacktestRunCoordinator
    from thesis.workflows.runtime import WorkflowRuntime

bearer_scheme = HTTPBearer(auto_error=False)

_V1_ISSUER_TEMPLATE = "https://sts.windows.net/{tenant}/"
_V2_ISSUER_TEMPLATE = "https://login.microsoftonline.com/{tenant}/v2.0"
_JWKS_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "unauthorized",
            "message": "a valid bearer token is required",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def authenticate_bearer(
    credentials: HTTPAuthorizationCredentials | None,
    expected_token: str | None,
) -> str:
    """Validate one configured bearer token using constant-time comparison."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    if expected_token is None or not compare_digest(
        credentials.credentials, expected_token
    ):
        raise _unauthorized()

    return credentials.credentials


def _looks_like_jwt(credential: str) -> bool:
    segments = credential.split(".")
    return len(segments) == 3 and all(segments)


def _non_empty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class EntraJwtAuthenticator:
    """Validates Entra managed-identity JWTs for the Thesis application.

    Signature verification uses the tenant JWKS discovery document, the
    audience must equal the Thesis application client ID, both the v1
    ``sts.windows.net`` issuer form (minted for managed identities) and the
    v2 ``login.microsoftonline.com`` form are accepted, and the configured
    application role must appear in the ``roles`` claim.
    """

    tenant_id: str
    audience: str
    role: str = "Thesis.Access"
    jwks_client: PyJWKClient | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "entra tenant id")
        _non_empty(self.audience, "entra audience")
        _non_empty(self.role, "entra role")

    @property
    def _issuers(self) -> tuple[str, str]:
        return (
            _V1_ISSUER_TEMPLATE.format(tenant=self.tenant_id),
            _V2_ISSUER_TEMPLATE.format(tenant=self.tenant_id),
        )

    def _signing_key(self, token: str) -> PyJWK:
        client = self.jwks_client
        if client is None:
            client = PyJWKClient(_JWKS_URL_TEMPLATE.format(tenant=self.tenant_id))
            object.__setattr__(self, "jwks_client", client)
        return client.get_signing_key_from_jwt(token)

    def authenticate(self, token: str) -> str:
        """Return the presented token after Entra validation, or raise 401."""
        try:
            signing_key = self._signing_key(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=list(self._issuers),
                options={"require": ["exp"]},
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError) as error:
            raise _unauthorized() from error
        roles = claims.get("roles", ())
        if isinstance(roles, str):
            roles = (roles,)
        if self.role not in roles:
            raise _unauthorized()
        return token


@dataclass(frozen=True, slots=True)
class CombinedAuthenticator:
    """FastAPI dependency accepting static bearer tokens and Entra JWTs.

    Without Entra configuration this dependency behaves like a plain static
    bearer check.  With Entra configuration a three-segment JWT credential is
    validated as an Entra token while any other bearer string continues to
    validate against the static token; invalid Entra tokens fail closed with
    the shared 401 error shape.
    """

    expected_token: str | None = field(repr=False)
    entra: EntraJwtAuthenticator | None = None

    def __post_init__(self) -> None:
        if self.expected_token is not None and not self.expected_token.strip():
            raise ValueError("bearer token must be a non-empty string")

    async def __call__(
        self,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
        ],
    ) -> str:
        """Return the presented token or raise a uniform authentication error."""
        if self.entra is None:
            return authenticate_bearer(credentials, self.expected_token)
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise _unauthorized()
        credential = credentials.credentials
        if _looks_like_jwt(credential):
            return self.entra.authenticate(credential)
        return authenticate_bearer(credentials, self.expected_token)


def _configured_token(bearer_token: str | None) -> str | None:
    token = (
        os.environ.get("THESIS_API__BEARER_TOKEN")
        if bearer_token is None
        else bearer_token
    )
    if token is not None and not token.strip():
        raise ValueError("bearer token must be a non-empty string")
    return token


def _backtest_tool_observer(
    coordinator: BacktestRunCoordinator,
) -> ToolResultObserver:
    """Build the tool-result observer that imports finished harness runs."""

    def observe(
        workspace_id: str,
        chat_id: str,
        execution_id: str | None,
        chunk: ToolResultChunk,
    ) -> ToolResultChunk:
        if chunk.tool not in ("run_backtest", "get_run") or not chunk.ok:
            return chunk
        result = HarnessToolResult(
            tool=chunk.tool,
            ok=chunk.ok,
            structured=dict(chunk.structured),
            text=chunk.text,
            failure=None,
        )
        enrichment = coordinator.handle_tool_result(
            workspace_id,
            result,
            chat_id=chat_id,
            execution_id=execution_id,
        )
        if not enrichment:
            return chunk
        return replace(chunk, structured={**dict(chunk.structured), **enrichment})

    return observe


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
    chat_service: ChatService | None = None,
    chat_stream_broker: InMemoryChatStreamBroker | None = None,
    chat_reply_service: ChatReplyService | None = None,
    entra_tenant_id: str | None = None,
    entra_audience: str | None = None,
    entra_role: str = "Thesis.Access",
    jwks_client: PyJWKClient | None = None,
    stream_poll_interval_seconds: float = 1.0,
    stream_sleeper: StreamSleeper | None = None,
    provider_delta_source: ProviderDeltaSource | None = None,
    stream_max_poll_cycles: int | None = None,
    screener_service: ScreenerService | None = None,
    workspace_data: WorkspaceDataBundle | None = None,
    workflow_runtime: WorkflowRuntime | None = None,
    harness_data_service: HarnessDataService | None = None,
    model_provider: OpenRouterProvider | None = None,
    harness_tool_provider: HarnessToolProvider | None = None,
) -> FastAPI:
    """Build the Thesis API app with injectable services and authentication.

    The chat and streaming routers resolve their :class:`ChatService` from
    ``app.state.chat_service``; pass ``chat_service`` to inject one, otherwise
    the first request constructs an in-memory service.  The screener router
    resolves its :class:`ScreenerService` from ``app.state.screener_service``;
    without an injected service the screener surface answers 503 (fail closed
    when no data source is configured).  The workspace DATA routers
    (workspaces, backtests, deployments, dashboards, screeners, and workspace
    state) resolve their services from ``app.state`` the same way; pass
    ``workspace_data`` — typically built by
    :func:`thesis.http.workspaces.build_local_workspace_data` — to compose the
    local-mode services.

    Pass ``model_provider`` (with an optional ``harness_tool_provider``) to
    compose the streamed chat reply service without injecting one directly.
    When a harness tool surface is configured the agent replies through the
    harness MCP tool catalog; an unavailable catalog degrades to a plain reply
    at request time, so composition never blocks startup.
    """
    entra_authenticator = (
        EntraJwtAuthenticator(
            tenant_id=entra_tenant_id,
            audience=entra_audience,
            role=entra_role,
            jwks_client=jwks_client,
        )
        if entra_tenant_id is not None and entra_audience is not None
        else None
    )
    authenticator = CombinedAuthenticator(
        _configured_token(bearer_token), entra=entra_authenticator
    )
    application = FastAPI(
        title="Thesis API",
        version="0.1.0",
        responses={
            status.HTTP_401_UNAUTHORIZED: {"description": "missing credentials"},
            422: {"description": "validation error"},
        },
    )
    backtest_coordinator: BacktestRunCoordinator | None = None
    if harness_tool_provider is not None and workspace_data is not None:
        from thesis.backtests import BacktestRunCoordinator

        backtest_coordinator = BacktestRunCoordinator(
            workspace_data.backtests, harness_tool_provider
        )
        application.state.backtest_run_coordinator = backtest_coordinator
    tool_result_observer = (
        _backtest_tool_observer(backtest_coordinator)
        if backtest_coordinator is not None
        else None
    )
    if chat_service is not None:
        application.state.chat_service = chat_service
    if chat_stream_broker is not None:
        application.state.chat_stream_broker = chat_stream_broker
    if chat_reply_service is not None:
        application.state.chat_reply_service = chat_reply_service
        if workflow_runtime is not None and chat_reply_service.workflow_runtime is None:
            chat_reply_service.workflow_runtime = workflow_runtime
        if (
            tool_result_observer is not None
            and chat_reply_service.tool_result_observer is None
        ):
            chat_reply_service.tool_result_observer = tool_result_observer
    if chat_reply_service is None and model_provider is not None:
        resolved_chat_service = chat_service
        if resolved_chat_service is None:
            resolved_chat_service = ChatService()
            application.state.chat_service = resolved_chat_service
        resolved_broker = chat_stream_broker
        if resolved_broker is None:
            resolved_broker = InMemoryChatStreamBroker()
            application.state.chat_stream_broker = resolved_broker
        application.state.chat_reply_service = ChatReplyService(
            chat_service=resolved_chat_service,
            broker=resolved_broker,
            provider=reply_provider_for(model_provider, harness_tool_provider),
            workflow_runtime=workflow_runtime,
            development_runtime=workflow_runtime,
            tool_result_observer=tool_result_observer,
        )
    if workspace_data is not None:
        application.state.workspace_service = workspace_data.workspaces
        application.state.backtest_service = workspace_data.backtests
        application.state.deployment_service = workspace_data.deployments
        application.state.dashboard_service = workspace_data.dashboards
        application.state.screener_data_service = workspace_data.screeners
        application.state.workspace_state_service = workspace_data.state
        application.state.strategy_service = workspace_data.strategies
        if workspace_data.harness is not None:
            application.state.harness_boundary = workspace_data.harness
    if screener_service is not None:
        application.state.screener_service = screener_service
    if workflow_runtime is not None:
        application.state.workflow_runtime = workflow_runtime
    if harness_data_service is not None:
        application.state.harness_data_service = harness_data_service

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

    @application.get("/health", tags=["health"])
    @application.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        """Return a probe-safe liveness response without requiring credentials."""
        return {"status": "ok"}

    @application.get(
        "/v1/auth/check",
        dependencies=[Depends(authenticator)],
        tags=["auth"],
    )
    async def auth_check() -> dict[str, str]:
        """Return a minimal protected probe for credential verification."""
        return {"status": "ok"}

    application.include_router(
        create_chat_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_streaming_router(
            poll_interval_seconds=stream_poll_interval_seconds,
            sleeper=stream_sleeper,
            provider_delta_source=provider_delta_source,
            max_poll_cycles=stream_max_poll_cycles,
        ),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_sub_agent_router(
            poll_interval_seconds=stream_poll_interval_seconds,
            sleeper=stream_sleeper,
            max_poll_cycles=stream_max_poll_cycles,
        ),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_workspaces_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_backtest_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_harness_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_deployment_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_dashboard_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_screeners_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_workspace_state_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_strategy_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_screener_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_knowledge_router(),
        dependencies=[Depends(authenticator)],
    )
    application.include_router(
        create_validation_router(),
        dependencies=[Depends(authenticator)],
    )
    return application


__all__ = [
    "CombinedAuthenticator",
    "EntraJwtAuthenticator",
    "authenticate_bearer",
    "bearer_scheme",
    "create_app",
]
