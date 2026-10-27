"""Composed Thesis HTTP application.

The app composes authentication, chat and execution REST routes, and the
unified chat timeline SSE router. Authentication
mirrors the market-lake ``CombinedAuthenticator`` pattern: a single static
bearer token, optional Entra JWT validation, a uniform 401 error shape, and
fail-closed behavior when no token is configured.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from secrets import compare_digest
from typing import TYPE_CHECKING, Annotated

import jwt
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWK, PyJWKClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from thesis.agent.harness_tools import (
    HarnessToolProvider,
    HarnessToolResult,
    open_harness_tool_provider,
)
from thesis.application.prompts import FCA_REPLY_SYSTEM_PROMPT
from thesis.auth.session import OperatorSessionService
from thesis.auth.workspace_access import WorkspaceAuthorizer
from thesis.http.artifacts import create_artifact_router
from thesis.http.auth import (
    CsrfTokenGuard,
    OidcVerifier,
    SessionCookieAuthenticator,
    create_auth_router,
)
from thesis.http.backtests import create_backtest_router
from thesis.http.chat import create_chat_router
from thesis.http.chat_sub_agents import create_chat_sub_agent_router
from thesis.http.dashboards import create_dashboard_router
from thesis.http.deployments import create_deployment_router
from thesis.http.errors import ErrorResponse
from thesis.http.harness import HarnessDataService, create_harness_router
from thesis.http.knowledge import create_knowledge_router
from thesis.http.screeners import create_screener_router
from thesis.http.state import create_workspace_state_router
from thesis.http.strategies import create_strategy_router
from thesis.http.streaming import StreamSleeper
from thesis.http.timeline import create_timeline_router
from thesis.http.workflows import create_workflow_router
from thesis.http.workspaces import (
    WorkspaceDataBundle,
    create_workspaces_router,
    manifest_owner_lookup,
    require_workspace_access,
)
from thesis.identity.ports import AccountStore, SessionStore
from thesis.persistence.investigations import open_investigations
from thesis.persistence.stack import open_submission_store
from thesis.research.investigations import Investigations, MemoryInvestigationStore
from thesis.screening.service import ScreenerService
from thesis_runtime.adapters.providers.families import model_family_adapter
from thesis_runtime.adapters.providers.openrouter import OpenRouterProvider
from thesis_runtime.adapters.providers.session_adapter import NativeSessionAdapter
from thesis_runtime.contracts.reply import ToolResultChunk, ToolResultObserver
from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.timeline import Timeline
from thesis_runtime.subagents.policy import OperatorPolicyRegistry

if TYPE_CHECKING:
    from thesis.application.workflows import WorkflowRuntime
    from thesis.backtests import BacktestRunCoordinator
    from thesis.http.workspaces import WorkspaceStore
    from thesis.integrations.mcp import HarnessMcpClient
    from thesis.workspaces.repository import WorkspaceRepositoryProvider
    from thesis_runtime.workflows.registry import WorkflowRegistry

bearer_scheme = HTTPBearer(auto_error=False)

_V1_ISSUER_TEMPLATE = "https://sts.windows.net/{tenant}/"
_V2_ISSUER_TEMPLATE = "https://login.microsoftonline.com/{tenant}/v2.0"
_JWKS_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"


def _utc_now() -> datetime:
    return datetime.now(UTC)


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

    def __call__(
        self,
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
        ],
    ) -> str:
        """Return the presented token or raise a uniform authentication error.

        The authenticated principal is also stored on the request state, so
        downstream routes can attribute an action to the authenticated operator
        instead of trusting a client-supplied identifier.
        """
        if self.entra is None:
            principal = authenticate_bearer(credentials, self.expected_token)
        elif credentials is None or credentials.scheme.lower() != "bearer":
            raise _unauthorized()
        elif _looks_like_jwt(credentials.credentials):
            principal = self.entra.authenticate(credentials.credentials)
        else:
            principal = authenticate_bearer(credentials, self.expected_token)
        request.state.authenticated_principal = principal
        return principal


def _configured_token(bearer_token: str | None) -> str | None:
    token = (
        os.environ.get("THESIS_API__BEARER_TOKEN")
        if bearer_token is None
        else bearer_token
    )
    if token is not None and not token.strip():
        raise ValueError("bearer token must be a non-empty string")
    return token


def _environment_value(name: str) -> str | None:
    """Return a stripped non-empty environment value, or ``None``."""
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _backtest_tool_observer(
    coordinator: BacktestRunCoordinator,
) -> ToolResultObserver:
    """Build the tool-result observer that imports finished harness jobs.

    The coordinator owns harness-versus-tracking classification, so this
    observer forwards every successful tool result and never duplicates the
    harness tool-name filter.
    """

    def observe(
        workspace_id: str,
        chat_id: str,
        execution_id: str | None,
        chunk: ToolResultChunk,
    ) -> ToolResultChunk:
        if not chunk.ok:
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


class ThesisApplication(FastAPI):
    """Publish the same source-derived contract as the offline schema exporter."""

    def openapi(self) -> dict:
        if self.openapi_schema is None:
            from thesis.http.schema import enrich_schema

            self.openapi_schema = enrich_schema(super().openapi())
        return self.openapi_schema


@cache
def _stateless_routers() -> tuple[APIRouter, ...]:
    """Reuse service-independent declarations; FastAPI binds copies per app.

    These endpoints resolve services from the request's application state.
    Authentication, workflow registration and streaming settings remain
    application-owned and are never cached here.
    """
    return (
        create_chat_router(),
        create_chat_sub_agent_router(),
        create_workspaces_router(),
        create_backtest_router(),
        create_harness_router(),
        create_deployment_router(),
        create_dashboard_router(),
        create_workspace_state_router(),
        create_strategy_router(),
        create_artifact_router(),
        create_screener_router(),
        create_knowledge_router(),
    )


def create_app(
    *,
    bearer_token: str | None = None,
    chat_service: ChatService | None = None,
    investigations: Investigations | None = None,
    conversations: Conversations | None = None,
    entra_tenant_id: str | None = None,
    entra_audience: str | None = None,
    entra_role: str = "Thesis.Access",
    jwks_client: PyJWKClient | None = None,
    stream_recovery_interval_seconds: float = 15.0,
    stream_sleeper: StreamSleeper | None = None,
    stream_max_poll_cycles: int | None = None,
    workspace_data: WorkspaceDataBundle | None = None,
    workflow_runtime: WorkflowRuntime | None = None,
    workflow_registry: WorkflowRegistry | None = None,
    harness_data_service: HarnessDataService | None = None,
    model_provider: OpenRouterProvider | None = None,
    harness_tool_provider: HarnessToolProvider | None = None,
    harness_client: HarnessMcpClient | None = None,
    account_store: AccountStore | None = None,
    auth_verifier: OidcVerifier | None = None,
    session_service: OperatorSessionService | None = None,
    session_store: SessionStore | None = None,
    session_workspace_id: str | None = None,
    session_clock: Callable[[], datetime] | None = None,
    timeline: Timeline | None = None,
    operator_policies: OperatorPolicyRegistry | None = None,
    in_memory: bool | None = None,
) -> FastAPI:
    """Build the Thesis API app with injectable services and authentication.

    The chat and timeline routers resolve their :class:`ChatService` from
    ``app.state.chat_service``. With no supplied container, explicit in-memory
    defaults are composed once here; ``in_memory=False`` disables them.
    Missing services fail closed rather than being created by requests. The workspace DATA
    routers (workspaces, backtests, deployments, dashboards, and workspace
    state) resolve their services from ``app.state`` the same way; pass
    ``workspace_data`` — typically built by
    :func:`thesis.http.workspaces.build_local_workspace_data` — to compose the
    local-mode services.

    A configured chat service always gets canonical conversation intake, even
    without a model provider; delivery-only intake must not start workers.
    Pass ``model_provider`` (with an optional ``harness_tool_provider``) to
    enable execution without injecting conversations directly.  The
    composed service runs root replies through the canonical session provider
    with an empty tool registry, and the harness tool surface is consumed by the
    backtest coordinator.  A ``harness_client`` shared with the backtest
    boundary is adapted into the tool provider without opening or
    initializing a second MCP transport.  ``app.state.operator_policies`` is
    always populated with the human-only default; pass ``operator_policies``
    to share the composition's registry with the conversations service.

    Pass ``account_store`` and ``auth_verifier`` together to register the
    anonymous OIDC login router.  Adding ``session_service``, ``session_store``,
    and ``session_workspace_id`` (with an optional ``session_clock``) issues a
    durable server-side session cookie on login and enables the session
    logout/revocation endpoints plus CSRF enforcement on the protected routers.
    The login router is registered on both the root and ``/api`` surfaces
    without the shared bearer dependency; every other route is unchanged.
    """
    if in_memory is None:
        in_memory = (
            chat_service is None and conversations is None and workspace_data is None
        )
    if chat_service is None and conversations is not None:
        chat_service = conversations.chat
    if conversations is not None and chat_service is not conversations.chat:
        raise ValueError("conversations must share the injected chat service")
    if chat_service is None and in_memory:
        chat_service = ChatService()
    if chat_service is not None and timeline is not None:
        chat_service.timeline = timeline
    if investigations is None and in_memory and chat_service is not None:
        investigations = Investigations(chat_service, MemoryInvestigationStore())
    if workspace_data is None and in_memory:
        from thesis.http.workspaces import build_local_workspace_data

        workspace_data = build_local_workspace_data(chat_service=chat_service)
    if workspace_data is not None and chat_service is not None:
        chat_service.bind_workspace_deletion(workspace_data.workspaces.store.is_deleted)
    if harness_data_service is None and in_memory:
        harness_data_service = HarnessDataService()
    if harness_tool_provider is None and harness_client is not None:
        harness_tool_provider = open_harness_tool_provider(client=harness_client)
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
    if (account_store is None) != (auth_verifier is None):
        raise ValueError("account_store and auth_verifier must be configured together")
    session_configured = (
        session_service is not None
        or session_store is not None
        or session_workspace_id is not None
    )
    if session_configured:
        if account_store is None or auth_verifier is None:
            raise ValueError(
                "session issuance requires account_store and auth_verifier"
            )
        if (
            session_service is None
            or session_store is None
            or session_workspace_id is None
        ):
            raise ValueError(
                "session_service, session_store, and session_workspace_id "
                "must be configured together"
            )
    resolved_session_clock = session_clock or _utc_now
    csrf_guard: CsrfTokenGuard | None = None
    primary_authenticator: CombinedAuthenticator | SessionCookieAuthenticator = (
        authenticator
    )
    if session_configured and session_store is not None:
        csrf_guard = CsrfTokenGuard(
            session_store=session_store, clock=resolved_session_clock
        )
        # Cookie sessions compose with, and never weaken, the bearer/Entra path:
        # a present cookie is authoritative and fails closed; an absent cookie
        # delegates unchanged to the existing authenticator.
        primary_authenticator = SessionCookieAuthenticator(
            session_store=session_store,
            fallback=authenticator,
            clock=resolved_session_clock,
        )
    auth_router: APIRouter | None = None
    if account_store is not None and auth_verifier is not None:
        auth_router = create_auth_router(
            account_store,
            auth_verifier,
            session_service=session_service,
            session_store=session_store,
            workspace_id=session_workspace_id,
            clock=resolved_session_clock,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            if workspace_data is not None and chat_service is not None:
                for (
                    workspace_id
                ) in workspace_data.workspaces.store.deleted_workspace_ids():
                    await asyncio.to_thread(
                        chat_service.delete_workspace_conversations, workspace_id
                    )
            yield
        finally:
            if timeline is not None:
                await asyncio.to_thread(timeline.commits.close)

    application = ThesisApplication(
        title="Thesis API",
        version="0.1.0",
        lifespan=lifespan,
        responses={
            code: {"model": ErrorResponse, "description": "request failed"}
            for code in (400, 401, 403, 404, 409, 422, 500, 502, 503)
        },
    )
    # Per-node operator policy is always present so the chat sub-agent router
    # fails closed to human-only instead of having no registry at all; a
    # composition may share its registry with the conversations service.
    application.state.operator_policies = (
        operator_policies if operator_policies is not None else OperatorPolicyRegistry()
    )
    backtest_coordinator: BacktestRunCoordinator | None = None
    if harness_tool_provider is not None and workspace_data is not None:
        from thesis.backtests import BacktestRunCoordinator

        backtest_coordinator = BacktestRunCoordinator(
            workspace_data.backtests.store,
            harness_tool_provider,
            tracking=None if chat_service is None else chat_service.find_tool_job,
        )
        application.state.backtest_run_coordinator = backtest_coordinator
    tool_result_observer = (
        _backtest_tool_observer(backtest_coordinator)
        if backtest_coordinator is not None
        else None
    )
    if chat_service is not None:
        application.state.chat_service = chat_service
    application.state.investigations = investigations
    if conversations is not None:
        application.state.conversations = conversations
        if (
            tool_result_observer is not None
            and conversations.tool_result_observer is None
        ):
            conversations.tool_result_observer = tool_result_observer
    if conversations is None and model_provider is not None and chat_service is None:
        raise ValueError("model_provider requires a configured chat service")
    if conversations is None and chat_service is not None:
        from thesis_runtime.tools.registry import SessionToolRegistry

        application.state.conversations = Conversations(
            chat=chat_service,
            provider=None
            if model_provider is None
            else NativeSessionAdapter(
                model_provider,
                family=model_family_adapter(model_provider.model),
                origin=(model_provider.provider_name, model_provider.model),
            ),
            registry_factory=lambda context: SessionToolRegistry(),
            root_system_prompt=FCA_REPLY_SYSTEM_PROMPT,
            tool_result_observer=tool_result_observer,
            read_consumed_input=chat_service.read_consumed_input,
            acknowledge_input=chat_service.acknowledge_input,
        )
    if workspace_data is not None:
        application.state.workspace_service = workspace_data.workspaces
        application.state.backtest_service = workspace_data.backtests
        application.state.submission_store = workspace_data.backtests.submissions
        application.state.deployment_service = workspace_data.deployments
        application.state.dashboard_service = workspace_data.dashboards
        application.state.workspace_state_service = workspace_data.state
        application.state.strategy_service = workspace_data.strategies
        application.state.artifact_catalog_service = workspace_data.strategies.catalog
        from thesis.backtests.catalog import BacktestCatalog

        application.state.backtest_catalog = BacktestCatalog(
            submissions=workspace_data.backtests.submissions,
            tracking=None if chat_service is None else chat_service.list_tool_jobs,
            reports=workspace_data.backtests.list_backtests,
            read_job=None
            if harness_data_service is None
            else harness_data_service.job_status,
            read_artifact=None
            if harness_data_service is None
            else harness_data_service.artifact,
            list_artifacts=None
            if harness_data_service is None
            else harness_data_service.list_artifacts,
        )
        workspace_data.workspaces.bind_count_provider(
            "backtests", application.state.backtest_catalog.count
        )
        screener_service = workspace_data.screeners
        if (
            screener_service is not None
            and screener_service.tools is None
            and harness_tool_provider is not None
        ):
            # Reuse the already-open MCP client so the screener surface is live
            # whenever the serving composition has a harness tool boundary, even
            # when the workspace bundle was built without one.
            screener_service = ScreenerService(
                tools=harness_tool_provider.client,
                store=screener_service.store,
                submissions=screener_service.submissions,
            )
        if screener_service is not None:
            application.state.screener_service = screener_service
        if workspace_data.harness is not None:
            application.state.harness_boundary = workspace_data.harness
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
                    details=detail.get(
                        "details",
                        {
                            key: value
                            for key, value in detail.items()
                            if key not in ("code", "message")
                        }
                        or None,
                    ),
                )
        return _error_response(
            exception.status_code,
            {
                400: "invalid_request",
                401: "unauthorized",
                403: "forbidden",
                404: "not_found",
                409: "conflict",
                422: "validation_error",
            }.get(exception.status_code, "http_error"),
            str(detail),
            headers=exception.headers,
            details=detail if not isinstance(detail, str) else None,
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

    @application.exception_handler(Exception)
    async def unexpected_exception_handler(
        request: Request, exception: Exception
    ) -> JSONResponse:
        import logging

        logging.getLogger(__name__).error(
            "Unhandled HTTP request failure", exc_info=exception
        )
        return _error_response(500, "internal_error", "request failed")

    @application.get("/health", tags=["health"])
    @application.get("/healthz", include_in_schema=False)
    @application.get("/api/health", tags=["health"])
    @application.get("/api/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        """Return a probe-safe liveness response without requiring credentials."""
        return {"status": "ok"}

    @application.get(
        "/api/v1/auth/check",
        dependencies=[Depends(primary_authenticator)],
        tags=["auth"],
    )
    @application.get(
        "/v1/auth/check",
        dependencies=[Depends(primary_authenticator)],
        tags=["auth"],
    )
    async def auth_check() -> dict[str, str]:
        """Return a minimal protected probe for credential verification."""
        return {"status": "ok"}

    routers = (
        *_stateless_routers(),
        create_timeline_router(
            recovery_interval_seconds=stream_recovery_interval_seconds,
            sleeper=stream_sleeper,
            max_poll_cycles=stream_max_poll_cycles,
        ),
        create_workflow_router(workflow_registry),
    )
    auth_dependency = [Depends(primary_authenticator)]
    if csrf_guard is not None:
        auth_dependency.append(Depends(csrf_guard))
    if workspace_data is not None:

        def require_live_workspace(request: Request) -> None:
            workspace_id = request.path_params.get("workspace_id")
            # DELETE remains retryable after a partial deletion. Every other
            # route, including chat, file and stream routes, shares this fence.
            deleting = request.method == "DELETE" and request.url.path.rstrip(
                "/"
            ).endswith("/workspaces/" + str(workspace_id))
            if (
                workspace_id
                and not deleting
                and workspace_data.workspaces.store.is_deleted(workspace_id)
            ):
                raise HTTPException(
                    status_code=404, detail="workspace has been deleted"
                )

        auth_dependency.append(Depends(require_live_workspace))
    # In the invite-only public mode (sessions + accounts configured), every
    # route is additionally workspace-authorized: a session's account id is
    # mapped to the workspace owner and cross-workspace access fails closed.
    # Bearer-only local/dev deployments keep their existing behavior because
    # there is no account identity to map.
    if workspace_data is not None and session_service is not None:
        workspace_authorizer = WorkspaceAuthorizer(
            manifest_owner_lookup(workspace_data.workspaces.store)
        )
        auth_dependency.append(
            Depends(
                require_workspace_access(primary_authenticator, workspace_authorizer)
            )
        )
    for router in routers:
        application.include_router(router, dependencies=auth_dependency)
        application.include_router(router, prefix="/api", dependencies=auth_dependency)

    # The auth router establishes its own credential (OIDC) and its session
    # endpoints carry their own cookie/CSRF dependencies, so it is registered
    # without the shared bearer dependency on both surfaces.
    if auth_router is not None:
        application.include_router(auth_router)
        application.include_router(auth_router, prefix="/api")

    # Both route surfaces resolve the same application state and handlers.
    if timeline is not None:
        application.state.timeline = timeline
    return application


def serving_workspace_store() -> WorkspaceStore | None:
    """Compose the workspace catalog shared by local and workspace runs.

    Azure mode combines managed-identity Blob catalog metadata with private
    Gitea repositories. Without a storage account, the original local
    filesystem catalog remains selected, regardless of Gitea settings.
    """
    account = _environment_value("THESIS_STORAGE_ACCOUNT_NAME")
    if account is None:
        return None
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    from thesis.http.workspaces import azure_workspace_store

    prefix = _environment_value("THESIS_WORKSPACE_CONTAINER_PREFIX") or "thesis-ws-"
    blob_service = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=DefaultAzureCredential(),
    )
    return azure_workspace_store(
        blob_service,
        container_prefix=prefix,
        repositories=serving_workspace_repositories(),
    )


def serving_workspace_repositories() -> WorkspaceRepositoryProvider | None:
    """Select remote checkouts only for Azure mode; local stays native Git."""
    if _environment_value("THESIS_STORAGE_ACCOUNT_NAME") is None:
        return None
    from tempfile import gettempdir

    from thesis.workspaces.gitea import GiteaWorkspaceProvider

    values = [
        _environment_value(name)
        for name in (
            "THESIS_GITEA_URL",
            "THESIS_GITEA_ORGANIZATION",
            "THESIS_GITEA_TOKEN",
        )
    ]
    if not any(values):
        return None
    if not all(values):
        raise ValueError(
            "Azure workspaces require THESIS_GITEA_URL, THESIS_GITEA_ORGANIZATION "
            "and THESIS_GITEA_TOKEN"
        )
    endpoint, organization, token = values
    assert endpoint is not None and organization is not None and token is not None
    root = _environment_value("THESIS_GITEA_CACHE_ROOT") or str(
        Path(gettempdir()) / "fca-thesis-gitea"
    )
    return GiteaWorkspaceProvider(root, endpoint, organization, token)


def serving_app_from_environment() -> FastAPI:
    """Compose the ASGI app ``thesis serve`` binds from the environment.

    Authentication is mandatory and fail-closed: the app is composed with a
    static bearer token (``THESIS_API__BEARER_TOKEN``), Entra JWT validation
    (``THESIS_API__ENTRA_TENANT_ID`` and ``THESIS_API__ENTRA_AUDIENCE``), or
    both.  A missing or half-configured authentication boundary raises a
    clear :class:`ValueError` instead of binding an unauthenticated server.

    Production explicitly disables in-memory defaults. Chat and workspace
    services are supplied by the durable composition; absent services fail
    closed with 503 rather than constructing request-local replacements.

    The serving stack composes the conversations service, control tools, and
    workflow runtime when ``OPENROUTER_API_KEY`` and ``HARNESS_MCP_URL`` are
    configured (see :mod:`thesis.application.composition`); the execution store then
    shares the same Cosmos database as the chat surface.
    """
    bearer_token = _environment_value("THESIS_API__BEARER_TOKEN")
    tenant_id = _environment_value("THESIS_API__ENTRA_TENANT_ID")
    audience = _environment_value("THESIS_API__ENTRA_AUDIENCE")
    role = _environment_value("THESIS_API__ENTRA_ROLE") or "Thesis.Access"
    if (tenant_id is None) != (audience is None):
        raise ValueError(
            "THESIS_API__ENTRA_TENANT_ID and THESIS_API__ENTRA_AUDIENCE "
            "must be configured together"
        )
    if bearer_token is None and tenant_id is None:
        raise ValueError(
            "authentication is not configured; set THESIS_API__BEARER_TOKEN "
            "or both THESIS_API__ENTRA_TENANT_ID and THESIS_API__ENTRA_AUDIENCE"
        )
    from thesis.application.composition import (
        build_serving_stack,
        open_serving_harness_tools,
        open_serving_model_provider,
    )
    from thesis.application.workflow_launcher import daemon_thread
    from thesis.http.workspaces import build_local_workspace_data
    from thesis_runtime.adapters.persistence import open_runtime_persistence

    persistence = open_runtime_persistence()
    investigations = open_investigations(persistence.chat)
    chat_service, timeline = persistence.chat, persistence.timeline
    submissions = open_submission_store()
    workspace_store = serving_workspace_store()
    repositories = getattr(workspace_store, "provider", None)
    from thesis.workspaces.local import default_workspace_root

    workspace_data = build_local_workspace_data(
        submissions=submissions,
        chat_service=chat_service,
        workspace_store=workspace_store,
        workspace_root=default_workspace_root() if workspace_store is None else None,
        workspace_repositories=repositories,
    )
    # The workflow runtime needs a model provider and a harness tool provider;
    # without them the stack stays chat-only and the durable message surface
    # still serves. The execution store is the same Cosmos database, so a
    # composed runtime persists one durable document per execution.
    model_provider = open_serving_model_provider()
    harness_tools = open_serving_harness_tools() if model_provider is not None else None
    stack = build_serving_stack(
        submissions=submissions,
        chat_service=chat_service,
        investigations=investigations,
        model_provider=model_provider,
        harness_tools=harness_tools,
        execution_store=persistence.executions,
        work_store=persistence.work,
        detach=daemon_thread,
        workspace_repositories=repositories,
    )
    application = create_app(
        bearer_token=bearer_token,
        entra_tenant_id=tenant_id,
        entra_audience=audience,
        entra_role=role,
        chat_service=stack.chat_service,
        investigations=investigations,
        workspace_data=workspace_data,
        conversations=stack.conversations,
        workflow_runtime=stack.workflow_runtime,
        workflow_registry=stack.workflow_registry,
        harness_tool_provider=stack.harness_tool_provider,
        harness_data_service=HarnessDataService(submissions=submissions),
        operator_policies=stack.operator_policies,
        timeline=timeline,
        in_memory=False,
    )
    from thesis.application.composition import build_serving_worker

    application.state.durable_worker = build_serving_worker(
        stack,
        persistence.work,
        persistence.ownership,
        persistence.executions,
        recovery_records=persistence.executions.identities,
    )
    return application


__all__ = [
    "CombinedAuthenticator",
    "EntraJwtAuthenticator",
    "authenticate_bearer",
    "bearer_scheme",
    "create_app",
    "serving_app_from_environment",
]
