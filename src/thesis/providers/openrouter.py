"""Fail-closed OpenRouter chat-completion adapter behind the provider protocol."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from thesis.providers.foundry import (
    CapabilityReason,
    ModelCapabilityStatus,
    ProviderAuthStatus,
    ProviderCapabilitySnapshot,
)
from thesis.providers.invocations import (
    ChatMessage,
    ModelCompletion,
    ProviderRejectedError,
    ProviderUnreachableError,
    Role,
    messages_payload,
    parse_chat_completion,
)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,127}$")


class EnvironmentKeySource(Protocol):
    """Process-resident secret source; keys are never stored or serialized."""

    def key(self) -> str: ...


class OpenRouterTransport(Protocol):
    """Narrow HTTP boundary; no network client is owned by the provider."""

    def post(
        self,
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]: ...


@dataclass(frozen=True, slots=True)
class OpenRouterProfile:
    """Non-secret OpenRouter configuration; the API key lives in the key source."""

    profile_id: str
    model: str
    base_url: str = DEFAULT_BASE_URL
    app_referer: str = "https://thesis.local/fca"
    app_title: str = "fca-thesis"
    enabled: bool = True

    @property
    def provider_name(self) -> str:
        return "openrouter"

    def __post_init__(self) -> None:
        for name in ("profile_id", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"{name} must be a portable identifier")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("OpenRouter base URL must be an HTTPS service URL")
        referer = urlsplit(self.app_referer)
        if referer.scheme != "https" or referer.hostname is None:
            raise ValueError("app_referer must be an HTTPS URL")
        if not isinstance(self.app_title, str) or not self.app_title.strip():
            raise ValueError("app_title must be a non-empty string")


class OpenRouterProvider:
    """Invoke OpenRouter chat completions with an injected key and transport."""

    __slots__ = ("_key_source", "_profile", "_transport")

    def __init__(
        self,
        profile: OpenRouterProfile,
        key_source: EnvironmentKeySource,
        transport: OpenRouterTransport,
    ) -> None:
        self._profile = profile
        self._key_source = key_source
        self._transport = transport

    @property
    def provider_name(self) -> str:
        return "openrouter"

    @property
    def profile_id(self) -> str:
        return self._profile.profile_id

    def __repr__(self) -> str:
        return (
            "OpenRouterProvider("
            f"profile_id={self.profile_id!r}, "
            f"base_url={self._profile.base_url!r}, "
            f"model={self._profile.model!r})"
        )

    def complete(
        self, *, messages: Sequence[ChatMessage], role: Role = "primary"
    ) -> ModelCompletion:
        """Run one chat completion; fail closed on network or rejection."""
        key = self._resolve_key()
        payload: dict[str, object] = {
            "model": self._profile.model,
            "messages": list(messages_payload(messages)),
        }
        status, body = self._post("/chat/completions", self._headers(key), payload)
        if not 200 <= status < 300:
            raise _rejected(status, body)
        return parse_chat_completion(
            body=body,
            provider=self.provider_name,
            fallback_model=self._profile.model,
            role=role,
        )

    def preflight(self) -> ProviderCapabilitySnapshot:
        """Probe availability without storing or exposing the API key."""
        if not self._profile.enabled:
            return self._snapshot(
                available=False,
                authenticated=False,
                provider_reason="not_configured",
                model_reason="provider_unavailable",
            )
        try:
            key = self._key_source.key()
            if not key:
                raise ValueError("key source returned an empty API key")
        except Exception:
            return self._snapshot(
                available=False,
                authenticated=False,
                provider_reason="authentication_unavailable",
                model_reason="provider_unavailable",
            )
        probe: dict[str, object] = {
            "model": self._profile.model,
            "messages": [{"content": "ping", "role": "user"}],
            "max_tokens": 1,
        }
        try:
            status, _ = self._transport.post(
                "/chat/completions", self._headers(key), probe
            )
        except Exception:
            return self._snapshot(
                available=False,
                authenticated=True,
                provider_reason="provider_unreachable",
                model_reason="provider_unavailable",
            )
        if status in (401, 403):
            return self._snapshot(
                available=False,
                authenticated=True,
                provider_reason="authentication_unavailable",
                model_reason="provider_unavailable",
            )
        if not 200 <= status < 300:
            return self._snapshot(
                available=False,
                authenticated=True,
                provider_reason="provider_unreachable",
                model_reason="provider_unavailable",
            )
        return self._snapshot(
            available=True,
            authenticated=True,
            provider_reason="ready",
            model_reason="ready",
        )

    def _resolve_key(self) -> str:
        try:
            key = self._key_source.key()
        except Exception as error:
            raise ProviderRejectedError(
                "openrouter api key is not available"
            ) from error
        if not key:
            raise ProviderRejectedError("openrouter api key is empty")
        return key

    def _headers(self, key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self._profile.app_referer,
            "X-Title": self._profile.app_title,
        }

    def _post(
        self,
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        try:
            return self._transport.post(path, headers, payload)
        except Exception as error:
            raise ProviderUnreachableError(
                "openrouter transport is unreachable"
            ) from error

    def _snapshot(
        self,
        *,
        available: bool,
        authenticated: bool,
        provider_reason: CapabilityReason,
        model_reason: CapabilityReason,
    ) -> ProviderCapabilitySnapshot:
        auth = ProviderAuthStatus(
            provider=self.provider_name,
            profile_id=self.profile_id,
            available=available,
            authenticated=authenticated,
            reason=provider_reason,
        )
        model = ModelCapabilityStatus(
            provider=self.provider_name,
            profile_id=self.profile_id,
            model_deployment=self._profile.model,
            available=available and provider_reason == "ready",
            reason=model_reason,
        )
        return ProviderCapabilitySnapshot(auth=auth, models=(model,))


def _rejected(status: int, body: str) -> ProviderRejectedError:
    message = _provider_message(body)
    detail = f": {message}" if message else ""
    return ProviderRejectedError(
        f"openrouter rejected the request (status {status}){detail}",
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
    "DEFAULT_BASE_URL",
    "EnvironmentKeySource",
    "OpenRouterProfile",
    "OpenRouterProvider",
    "OpenRouterTransport",
]
