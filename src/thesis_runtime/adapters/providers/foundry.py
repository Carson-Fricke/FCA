"""Credential-injected Azure AI Foundry capability preflight."""

from __future__ import annotations

import json
import re
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx

from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    ProviderRejectedError,
    ProviderUnreachableError,
    Role,
    messages_payload,
    parse_chat_completion,
)
from thesis_runtime.contracts.completion import CompletionOptions

CapabilityReason = Literal[
    "ready",
    "not_configured",
    "authentication_unavailable",
    "provider_unreachable",
    "provider_unavailable",
    "model_unavailable",
]
CredentialMode = Literal["default_azure_credential", "managed_identity"]

_FOUNDRY_SCOPE = "https://ai.azure.com/.default"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EntraAccessToken(Protocol):
    """Minimal process-local access token returned by an Entra credential."""

    @property
    def token(self) -> str: ...

    @property
    def expires_on(self) -> int: ...


class EntraCredential(Protocol):
    """Backend-only Azure Identity credential contract."""

    def get_token(self, *scopes: str) -> EntraAccessToken: ...


@dataclass(frozen=True, slots=True)
class FoundryProviderProfile:
    """Non-secret Azure AI Foundry deployment configuration."""

    profile_id: str
    endpoint: str
    project_name: str
    model_deployment: str
    credential_mode: CredentialMode
    enabled: bool = True
    tenant_id: str | None = None
    managed_identity_client_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("profile_id", "project_name", "model_deployment"):
            value = getattr(self, name)
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"{name} must be a portable identifier")
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Foundry endpoint must be an HTTPS service URL")
        for name in ("tenant_id", "managed_identity_client_id"):
            value = getattr(self, name)
            if value is not None and (not value or value != value.strip()):
                raise ValueError(f"{name} must be non-empty and trimmed when set")


class FoundryDeploymentClient(Protocol):
    """Narrow adapter over the backend Foundry deployment API."""

    def deployment_available(
        self, *, project_name: str, deployment_name: str
    ) -> bool: ...


class FoundryClientFactory(Protocol):
    """Construct a backend client from a profile and process credential."""

    def __call__(
        self, *, profile: FoundryProviderProfile, credential: EntraCredential
    ) -> FoundryDeploymentClient: ...


@dataclass(frozen=True, slots=True)
class ProviderAuthStatus:
    """Serializable provider status that intentionally excludes credentials."""

    provider: str
    profile_id: str | None
    available: bool
    authenticated: bool
    reason: CapabilityReason
    schema_version: Literal[1] = 1


@dataclass(frozen=True, slots=True)
class ModelCapabilityStatus:
    """Availability of one configured model deployment."""

    provider: str
    profile_id: str | None
    model_deployment: str | None
    available: bool
    reason: CapabilityReason
    schema_version: Literal[1] = 1


@dataclass(frozen=True, slots=True)
class ProviderCapabilitySnapshot:
    """One provider's redacted authentication and model probe results."""

    auth: ProviderAuthStatus
    models: tuple[ModelCapabilityStatus, ...]
    schema_version: Literal[1] = 1


class ModelProvider(Protocol):
    """Injected provider boundary consumed by runtime startup."""

    @property
    def provider_name(self) -> str: ...

    @property
    def profile_id(self) -> str: ...

    def preflight(self) -> ProviderCapabilitySnapshot: ...


@dataclass(frozen=True, slots=True)
class RuntimeCapabilityReport:
    """Startup capabilities; offline workspace access is invariant."""

    provider: ProviderAuthStatus
    models: tuple[ModelCapabilityStatus, ...]
    offline_workspace_available: Literal[True] = field(init=False, default=True)
    schema_version: Literal[1] = 1

    @property
    def model_backed_capabilities_available(self) -> bool:
        """Whether at least one configured model can run autonomous work."""
        return self.provider.available and any(model.available for model in self.models)


class FoundryProvider:
    """Probe Foundry using an injected Entra credential and client adapter."""

    __slots__ = ("_client_factory", "_credential", "_profile")

    def __init__(
        self,
        profile: FoundryProviderProfile,
        credential: EntraCredential,
        client_factory: FoundryClientFactory,
    ) -> None:
        self._profile = profile
        self._credential = credential
        self._client_factory = client_factory

    @property
    def provider_name(self) -> str:
        return "azure_ai_foundry"

    @property
    def profile_id(self) -> str:
        return self._profile.profile_id

    def __repr__(self) -> str:
        return (
            "FoundryProvider("
            f"profile_id={self.profile_id!r}, "
            f"endpoint={self._profile.endpoint!r}, "
            f"model_deployment={self._profile.model_deployment!r})"
        )

    def preflight(self) -> ProviderCapabilitySnapshot:
        """Authenticate and verify deployment availability without leaking errors."""
        if not self._profile.enabled:
            return self._snapshot(
                provider_available=False,
                authenticated=False,
                provider_reason="not_configured",
                model_available=False,
                model_reason="provider_unavailable",
            )

        try:
            access_token = self._credential.get_token(_FOUNDRY_SCOPE)
            if not access_token.token:
                raise ValueError("credential returned an empty access token")
        except Exception:
            return self._snapshot(
                provider_available=False,
                authenticated=False,
                provider_reason="authentication_unavailable",
                model_available=False,
                model_reason="provider_unavailable",
            )
        finally:
            access_token = None

        try:
            client = self._client_factory(
                profile=self._profile, credential=self._credential
            )
            model_available = client.deployment_available(
                project_name=self._profile.project_name,
                deployment_name=self._profile.model_deployment,
            )
        except Exception:
            return self._snapshot(
                provider_available=False,
                authenticated=True,
                provider_reason="provider_unreachable",
                model_available=False,
                model_reason="provider_unavailable",
            )

        return self._snapshot(
            provider_available=True,
            authenticated=True,
            provider_reason="ready",
            model_available=model_available,
            model_reason="ready" if model_available else "model_unavailable",
        )

    def _snapshot(
        self,
        *,
        provider_available: bool,
        authenticated: bool,
        provider_reason: CapabilityReason,
        model_available: bool,
        model_reason: CapabilityReason,
    ) -> ProviderCapabilitySnapshot:
        auth = ProviderAuthStatus(
            provider=self.provider_name,
            profile_id=self.profile_id,
            available=provider_available,
            authenticated=authenticated,
            reason=provider_reason,
        )
        model = ModelCapabilityStatus(
            provider=self.provider_name,
            profile_id=self.profile_id,
            model_deployment=self._profile.model_deployment,
            available=model_available,
            reason=model_reason,
        )
        return ProviderCapabilitySnapshot(auth=auth, models=(model,))


def runtime_capability_preflight(
    provider: ModelProvider | None,
) -> RuntimeCapabilityReport:
    """Probe an injected provider while keeping the offline workspace usable."""
    if provider is None:
        return RuntimeCapabilityReport(
            provider=ProviderAuthStatus(
                provider="unconfigured",
                profile_id=None,
                available=False,
                authenticated=False,
                reason="not_configured",
            ),
            models=(),
        )
    try:
        snapshot = provider.preflight()
    except Exception:
        snapshot = ProviderCapabilitySnapshot(
            auth=ProviderAuthStatus(
                provider=provider.provider_name,
                profile_id=provider.profile_id,
                available=False,
                authenticated=False,
                reason="provider_unreachable",
            ),
            models=(
                ModelCapabilityStatus(
                    provider=provider.provider_name,
                    profile_id=provider.profile_id,
                    model_deployment=None,
                    available=False,
                    reason="provider_unavailable",
                ),
            ),
        )
    return RuntimeCapabilityReport(provider=snapshot.auth, models=snapshot.models)


_FOUNDRY_API_VERSION = "2024-05-01-preview"


class FoundryInvocationTransport(Protocol):
    """Narrow HTTP boundary for deployment-bound chat completions."""

    def post(
        self, url: str, headers: Mapping[str, str], payload: Mapping[str, object]
    ) -> tuple[int, str]: ...


class FoundryDeploymentInvoker:
    """Deployment-bound chat completions composed with the existing preflight.

    The bearer token is resolved per call through the injected Entra credential
    and is never stored on the invoker or included in any serializable record.
    """

    __slots__ = (
        "_api_version",
        "_client_factory",
        "_credential",
        "_profile",
        "_transport",
    )

    streaming = False

    def __init__(
        self,
        profile: FoundryProviderProfile,
        credential: EntraCredential,
        transport: FoundryInvocationTransport,
        *,
        api_version: str = _FOUNDRY_API_VERSION,
        client_factory: FoundryClientFactory | None = None,
    ) -> None:
        self._profile = profile
        self._credential = credential
        self._transport = transport
        self._api_version = api_version
        self._client_factory = client_factory

    @property
    def provider_name(self) -> str:
        return "azure_ai_foundry"

    @property
    def profile_id(self) -> str:
        return self._profile.profile_id

    def __repr__(self) -> str:
        return (
            "FoundryDeploymentInvoker("
            f"profile_id={self.profile_id!r}, "
            f"endpoint={self._profile.endpoint!r}, "
            f"model_deployment={self._profile.model_deployment!r})"
        )

    def stream(
        self,
        *,
        messages: Sequence[ProviderMessage],
        role: Role = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
        options: CompletionOptions | None = None,
    ) -> Generator[ModelCompletion, None, None]:
        """This deployment port provides a single completed frame."""
        if tools or response_format:
            raise ProviderRejectedError(
                "deployment does not support tool or format options"
            )
        yield self.complete(messages=messages, role=role, options=options)

    def complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
        role: Role = "primary",
        options: CompletionOptions | None = None,
    ) -> ModelCompletion:
        """Run one deployment-bound chat completion, failing closed."""
        token = self._resolve_token()
        headers = {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }
        payload: dict[str, object] = {
            "model": self._profile.model_deployment,
            "messages": list(messages_payload(messages)),
        }
        if options:
            payload["max_tokens"] = options.max_output_tokens
            if not options.allow_tool_calls:
                payload["tool_choice"] = "none"
        status, body = self._post(self._invocation_url(), headers, payload)
        if not 200 <= status < 300:
            raise _rejected(status, body)
        return parse_chat_completion(
            body=body,
            provider=self.provider_name,
            fallback_model=self._profile.model_deployment,
            role=role,
        )

    def preflight(self) -> ProviderCapabilitySnapshot:
        """Compose the existing credential-injected preflight snapshot."""
        if self._client_factory is None:
            raise RuntimeError("client factory is required to compose preflight")
        return FoundryProvider(
            self._profile, self._credential, self._client_factory
        ).preflight()

    def _resolve_token(self) -> EntraAccessToken:
        try:
            token = self._credential.get_token(_FOUNDRY_SCOPE)
        except Exception as error:
            raise ProviderRejectedError(
                "foundry credential is not available"
            ) from error
        if not token.token:
            raise ProviderRejectedError("credential returned an empty access token")
        return token

    def _invocation_url(self) -> str:
        return (
            f"{self._profile.endpoint}/openai/deployments/"
            f"{self._profile.model_deployment}/chat/completions"
            f"?api-version={self._api_version}"
        )

    def _post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        try:
            return self._transport.post(url, headers, payload)
        except (httpx.TransportError, OSError) as error:
            raise ProviderUnreachableError(
                "foundry transport is unreachable"
            ) from error


def _rejected(status: int, body: str) -> ProviderRejectedError:
    message = _provider_message(body)
    detail = f": {message}" if message else ""
    return ProviderRejectedError(
        f"foundry rejected the request (status {status}){detail}",
        status=status,
    )


def _provider_message(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    if isinstance(error, str):
        return error
    return ""


__all__ = [
    "EntraAccessToken",
    "EntraCredential",
    "FoundryClientFactory",
    "FoundryDeploymentClient",
    "FoundryDeploymentInvoker",
    "FoundryInvocationTransport",
    "FoundryProvider",
    "FoundryProviderProfile",
    "ModelCapabilityStatus",
    "ModelProvider",
    "ProviderAuthStatus",
    "ProviderCapabilitySnapshot",
    "RuntimeCapabilityReport",
    "runtime_capability_preflight",
]
