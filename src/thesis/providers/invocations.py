"""Immutable invocation records, typed provider errors, and role bindings."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

Role = Literal["primary", "coding", "critique", "research"]
_ROLES = frozenset(("primary", "coding", "critique", "research"))
_CHAT_ROLES = frozenset(("system", "user", "assistant", "tool"))
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _digest(value: str, name: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")


def content_digest(value: bytes | str | object) -> str:
    """Return a deterministic SHA-256 digest for content or JSON values."""
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ProviderUnreachableError(RuntimeError):
    """Network-level failure while contacting a model provider."""


class ProviderRejectedError(RuntimeError):
    """The provider refused a request or returned an unusable response."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One chat message sent to a provider; carries no credentials."""

    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in _CHAT_ROLES:
            raise ValueError(f"unsupported chat message role: {self.role}")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("chat message content must be non-empty")


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    """Typed completion result; carries no credentials or raw payloads."""

    content: str
    model: str
    provider: str
    role: Role
    prompt_tokens: int
    completion_tokens: int
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ValueError("completion content must be text")
        for name in ("model", "provider"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.role not in _ROLES:
            raise ValueError(f"unsupported Thesis role: {self.role}")
        for name in ("prompt_tokens", "completion_tokens"):
            tokens = getattr(self, name)
            if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
                raise ValueError(f"{name} must be a non-negative integer")


def parse_chat_completion(
    *,
    body: str,
    provider: str,
    fallback_model: str,
    role: Role,
) -> ModelCompletion:
    """Parse an OpenAI-compatible chat completion body, failing closed."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError) as error:
        raise ProviderRejectedError(
            "provider returned a malformed JSON body"
        ) from error
    if not isinstance(data, dict):
        raise ProviderRejectedError("provider returned a malformed JSON body")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderRejectedError("provider response did not contain a completion")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ProviderRejectedError("provider response did not contain text content")
    usage = data.get("usage")
    tokens = _usage_tokens(usage if isinstance(usage, dict) else {})
    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        model = fallback_model
    return ModelCompletion(
        content=content,
        model=model,
        provider=provider,
        role=role,
        prompt_tokens=tokens[0],
        completion_tokens=tokens[1],
    )


def _usage_tokens(usage: Mapping[str, object]) -> tuple[int, int]:
    values: list[int] = []
    for name in ("prompt_tokens", "completion_tokens"):
        value = usage.get(name, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ProviderRejectedError("provider usage tokens are not integers")
        values.append(value)
    return values[0], values[1]


@dataclass(frozen=True, slots=True)
class ModelInvocation:
    """Redacted immutable record for one model invocation attempt."""

    provider: str
    model: str
    version: str
    role: Role
    request_digest: str
    response_digest: str
    created_at: datetime
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        for name in ("provider", "model", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.role not in _ROLES:
            raise ValueError(f"unsupported Thesis role: {self.role}")
        _digest(self.request_digest, "request_digest")
        _digest(self.response_digest, "response_digest")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("invocation timestamp must include a timezone")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))

    def as_dict(self) -> dict[str, object]:
        """Return a portable redacted representation without any credentials."""
        return {
            "createdAt": self.created_at.isoformat().replace("+00:00", "Z"),
            "model": self.model,
            "provider": self.provider,
            "requestDigest": self.request_digest,
            "responseDigest": self.response_digest,
            "role": self.role,
            "schemaVersion": self.schema_version,
            "version": self.version,
        }


class InvocationLedger(Protocol):
    """Append-only provenance store for model invocations."""

    def record(
        self,
        *,
        provider: str,
        model: str,
        version: str,
        role: Role,
        request_digest: str,
        response_digest: str,
    ) -> ModelInvocation: ...

    def list(self) -> tuple[ModelInvocation, ...]: ...


class InMemoryInvocationLedger:
    """Clock-injected in-memory ledger; holds digests only, never secrets."""

    __slots__ = ("_clock", "_records")

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = _utc_now if clock is None else clock
        self._records: list[ModelInvocation] = []

    def record(
        self,
        *,
        provider: str,
        model: str,
        version: str,
        role: Role,
        request_digest: str,
        response_digest: str,
    ) -> ModelInvocation:
        invocation = ModelInvocation(
            provider=provider,
            model=model,
            version=version,
            role=role,
            request_digest=request_digest,
            response_digest=response_digest,
            created_at=self._clock(),
        )
        self._records.append(invocation)
        return invocation

    def list(self) -> tuple[ModelInvocation, ...]:
        return tuple(self._records)


class ProviderProfileView(Protocol):
    """Minimal read-only view over any provider profile."""

    @property
    def profile_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RoleBinding:
    """Pre-result assignment of one role to one provider deployment."""

    role: Role
    provider: str
    profile_id: str
    model: str
    assigned_at: datetime
    assignment_digest: str
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError(f"unsupported Thesis role: {self.role}")
        for name in ("provider", "profile_id", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.assigned_at.tzinfo is None or self.assigned_at.utcoffset() is None:
            raise ValueError("assignment timestamp must include a timezone")
        _digest(self.assignment_digest, "assignment_digest")
        object.__setattr__(self, "assigned_at", self.assigned_at.astimezone(UTC))

    def as_dict(self) -> dict[str, object]:
        """Return a portable redacted representation without any credential."""
        return {
            "assignedAt": self.assigned_at.isoformat().replace("+00:00", "Z"),
            "assignmentDigest": self.assignment_digest,
            "model": self.model,
            "profileId": self.profile_id,
            "provider": self.provider,
            "role": self.role,
            "schemaVersion": self.schema_version,
        }


def bind_role(
    role: Role | str,
    provider_profile: ProviderProfileView,
    *,
    provider: str | None = None,
    model: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> RoleBinding:
    """Bind a role to a provider profile before any result is observed."""
    if role not in _ROLES:
        raise ValueError(f"unsupported Thesis role: {role}")
    provider_name = provider or getattr(provider_profile, "provider_name", None)
    model_name = model or (
        getattr(provider_profile, "model_deployment", None)
        or getattr(provider_profile, "model", None)
    )
    if not isinstance(provider_name, str) or not provider_name.strip():
        raise ValueError("provider must be resolvable for the role binding")
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError("model must be resolvable for the role binding")
    assigned_at = _utc_now() if clock is None else clock()
    return RoleBinding(
        role=role,
        provider=provider_name,
        profile_id=provider_profile.profile_id,
        model=model_name,
        assigned_at=assigned_at,
        assignment_digest=content_digest(
            {
                "assignedAt": assigned_at.astimezone(UTC).isoformat(),
                "model": model_name,
                "profileId": provider_profile.profile_id,
                "provider": provider_name,
                "role": role,
            }
        ),
    )


def messages_payload(messages: Sequence[ChatMessage]) -> tuple[dict[str, str], ...]:
    """Serialize chat messages into the wire-level payload tuple."""
    return tuple(
        {"role": message.role, "content": message.content} for message in messages
    )


__all__ = [
    "ChatMessage",
    "InMemoryInvocationLedger",
    "InvocationLedger",
    "ModelCompletion",
    "ModelInvocation",
    "ProviderProfileView",
    "ProviderRejectedError",
    "ProviderUnreachableError",
    "RoleBinding",
    "Role",
    "bind_role",
    "content_digest",
    "messages_payload",
    "parse_chat_completion",
]
