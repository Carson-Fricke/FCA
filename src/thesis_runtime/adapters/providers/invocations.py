"""Immutable invocation records, typed provider errors, and role bindings."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from thesis_runtime.contracts.errors import (
    ProviderRejectedError,
    ProviderUnreachableError,
)

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


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One model-requested function call in OpenAI ``message.tool_calls`` form.

    ``arguments`` is the raw JSON-encoded string the model emitted; callers
    decode it at the tool boundary so a malformed payload becomes a tool error
    instead of a provider failure.
    """

    id: str
    name: str
    arguments: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("tool call id must be non-empty text")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool call name must be non-empty text")
        if not isinstance(self.arguments, str):
            raise ValueError("tool call arguments must be a JSON string")

    def as_wire(self) -> dict[str, object]:
        """Return the OpenAI wire shape for this call."""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One chat message sent to a provider; carries no credentials.

    ``tool_calls`` is set on an assistant message that requested functions and
    ``tool_call_id`` on the matching ``tool`` role result.  Assistant messages
    that only request tools may carry empty content.
    """

    role: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in _CHAT_ROLES:
            raise ValueError(f"unsupported chat message role: {self.role}")
        if not isinstance(self.content, str):
            raise ValueError("chat message content must be text")
        for call in self.tool_calls:
            if not isinstance(call, ToolCall):
                raise ValueError("tool_calls must contain ToolCall values")
        if self.role == "tool":
            if not isinstance(self.tool_call_id, str) or not self.tool_call_id.strip():
                raise ValueError("tool messages require a non-empty tool_call_id")
        elif self.tool_call_id is not None:
            raise ValueError("only tool messages may carry a tool_call_id")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may carry tool_calls")
        if not self.content.strip() and not self.tool_calls:
            raise ValueError("chat message content must be non-empty")


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    """Typed completion result; carries no credentials or raw payloads.

    ``reasoning`` is the model's chain-of-thought side channel, kept strictly
    separate from ``content`` so a caller can surface it on its own channel
    without ever mixing it into the assistant answer.
    """

    content: str
    model: str
    provider: str
    role: Role
    prompt_tokens: int
    completion_tokens: int
    reasoning: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    schema_version: Literal[1] = 1
    upstream_provider: str | None = None
    generation_id: str | None = None
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ValueError("completion content must be text")
        if not isinstance(self.reasoning, str):
            raise ValueError("completion reasoning must be text")
        for call in self.tool_calls:
            if not isinstance(call, ToolCall):
                raise ValueError("tool_calls must contain ToolCall values")
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
    if not isinstance(message, dict):
        raise ProviderRejectedError("provider response did not contain a message")
    # ``reasoning``/``reasoning_content`` are a separate, explicitly-surfaced
    # channel: they populate ``ModelCompletion.reasoning`` and never enter the
    # assistant ``content``.  A malformed (non-string) reasoning value is
    # ignored rather than treated as a provider failure.
    content = message.get("content")
    reasoning = _parse_reasoning(message)
    tool_calls = _parse_tool_calls(message.get("tool_calls"))
    if isinstance(content, str):
        text = content
    elif content is None:
        text = ""
    else:
        raise ProviderRejectedError("provider response did not contain text content")
    usage = data.get("usage")
    tokens = _usage_tokens(usage if isinstance(usage, dict) else {})
    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        model = fallback_model
    return ModelCompletion(
        content=text,
        model=model,
        provider=provider,
        role=role,
        prompt_tokens=tokens[0],
        completion_tokens=tokens[1],
        reasoning=reasoning,
        tool_calls=tool_calls,
        upstream_provider=data.get("provider")
        if isinstance(data.get("provider"), str)
        else None,
        generation_id=data.get("id") if isinstance(data.get("id"), str) else None,
        finish_reason=choice.get("finish_reason")
        if isinstance(choice.get("finish_reason"), str)
        else None,
    )


def _parse_reasoning(message: Mapping[str, object]) -> str:
    """Read a string reasoning field, ignoring malformed or absent values.

    Both the ``reasoning`` and ``reasoning_content`` field names are accepted;
    the first non-empty string wins, and every non-string value is ignored so a
    provider quirk never becomes a reply failure.
    """
    for name in ("reasoning", "reasoning_content"):
        value = message.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def _parse_tool_calls(value: object) -> tuple[ToolCall, ...]:
    """Parse OpenAI ``message.tool_calls``, failing closed on malformed entries."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ProviderRejectedError("provider tool_calls must be a list")
    calls: list[ToolCall] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ProviderRejectedError("provider tool_call must be an object")
        call_id = entry.get("id")
        function = entry.get("function")
        if not isinstance(call_id, str) or not call_id.strip():
            raise ProviderRejectedError("provider tool_call is missing an id")
        if not isinstance(function, Mapping):
            raise ProviderRejectedError("provider tool_call is missing a function")
        name = function.get("name")
        arguments = function.get("arguments", "")
        if not isinstance(name, str) or not name.strip():
            raise ProviderRejectedError("provider tool_call is missing a name")
        if arguments is None:
            arguments = ""
        if not isinstance(arguments, str):
            raise ProviderRejectedError(
                "provider tool_call arguments must be a JSON string"
            )
        calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return tuple(calls)


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


def messages_payload(
    messages: Sequence[ChatMessage],
) -> tuple[dict[str, object], ...]:
    """Serialize chat messages into the wire-level payload tuple."""
    payload: list[dict[str, object]] = []
    for message in messages:
        entry: dict[str, object] = {
            "role": message.role,
            "content": message.content,
        }
        if message.tool_calls:
            entry["tool_calls"] = [call.as_wire() for call in message.tool_calls]
        if message.tool_call_id is not None:
            entry["tool_call_id"] = message.tool_call_id
        payload.append(entry)
    return tuple(payload)


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
    "ToolCall",
    "bind_role",
    "content_digest",
    "messages_payload",
    "parse_chat_completion",
]
