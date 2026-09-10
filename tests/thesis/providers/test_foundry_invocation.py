from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import pytest

from thesis.providers import (
    ChatMessage,
    EntraCredential,
    FoundryDeploymentInvoker,
    FoundryProvider,
    FoundryProviderProfile,
    InMemoryInvocationLedger,
    ProviderCapabilitySnapshot,
    ProviderRejectedError,
    ProviderUnreachableError,
    bind_role,
    content_digest,
)

SECRET = "secret-access-token-must-not-escape"
FIXED_NOW = datetime(2026, 2, 1, 8, 30, 0, tzinfo=UTC)

RESPONSE = json.dumps(
    {
        "id": "chatcmpl-foundry-1",
        "model": "reasoning-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "foundry answer"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18},
    }
)


@dataclass(frozen=True)
class FakeAccessToken:
    token: str = SECRET
    expires_on: int = 2_000_000_000


class FakeCredential:
    def __init__(self, error: Exception | None = None, token: str = SECRET) -> None:
        self.error = error
        self.token_value = token
        self.scopes: list[tuple[str, ...]] = []

    def get_token(self, *scopes: str) -> FakeAccessToken:
        self.scopes.append(scopes)
        if self.error is not None:
            raise self.error
        return FakeAccessToken(token=self.token_value)


class FakeClient:
    def __init__(self, available: bool = True) -> None:
        self.available = available

    def deployment_available(self, *, project_name: str, deployment_name: str) -> bool:
        return self.available


class FakeClientFactory:
    def __init__(self, client: FakeClient | None = None) -> None:
        self.client = FakeClient() if client is None else client

    def __call__(
        self, *, profile: FoundryProviderProfile, credential: EntraCredential
    ) -> FakeClient:
        assert profile.endpoint.startswith("https://")
        return self.client


class FakeTransport:
    def __init__(
        self, status: int = 200, body: str = RESPONSE, error: Exception | None = None
    ) -> None:
        self.status = status
        self.body = body
        self.error = error
        self.calls: list[tuple[str, Mapping[str, str], Mapping[str, object]]] = []

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        self.calls.append((url, dict(headers), dict(payload)))
        if self.error is not None:
            raise self.error
        return self.status, self.body


def _profile() -> FoundryProviderProfile:
    return FoundryProviderProfile(
        profile_id="developer",
        endpoint="https://example.services.ai.azure.com",
        project_name="thesis-project",
        model_deployment="reasoning-model",
        credential_mode="default_azure_credential",
    )


def _messages() -> tuple[ChatMessage, ...]:
    return (
        ChatMessage(role="system", content="thesis system prompt"),
        ChatMessage(role="user", content="analyze the hypothesis"),
    )


def test_happy_path_posts_deployment_url_with_bearer_token() -> None:
    transport = FakeTransport()
    invoker = FoundryDeploymentInvoker(
        _profile(), FakeCredential(), transport, client_factory=FakeClientFactory()
    )

    completion = invoker.complete(messages=_messages(), role="critique")

    url, headers, payload = transport.calls[0]
    assert url == (
        "https://example.services.ai.azure.com/openai/deployments/"
        "reasoning-model/chat/completions?api-version=2024-05-01-preview"
    )
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert headers["Content-Type"] == "application/json"
    assert payload["model"] == "reasoning-model"
    assert payload["messages"] == [
        {"role": "system", "content": "thesis system prompt"},
        {"role": "user", "content": "analyze the hypothesis"},
    ]
    assert completion.content == "foundry answer"
    assert completion.model == "reasoning-model"
    assert completion.provider == "azure_ai_foundry"
    assert completion.role == "critique"
    assert completion.prompt_tokens == 13
    assert completion.completion_tokens == 5


def test_token_is_resolved_per_invocation_and_never_stored() -> None:
    credential = FakeCredential()
    transport = FakeTransport()
    invoker = FoundryDeploymentInvoker(
        _profile(), credential, transport, client_factory=FakeClientFactory()
    )

    invoker.complete(messages=_messages())
    invoker.complete(messages=_messages())

    assert credential.scopes == [
        ("https://ai.azure.com/.default",),
        ("https://ai.azure.com/.default",),
    ]
    assert SECRET not in repr(invoker)
    assert SECRET not in json.dumps(dict(transport.calls[0][2]))
    assert SECRET in json.dumps(dict(transport.calls[0][1]))
    stored = [getattr(invoker, slot) for slot in invoker.__slots__]
    assert SECRET not in repr(stored)


@pytest.mark.parametrize("status", [401, 429, 503])
def test_non_2xx_responses_fail_closed_with_typed_error(status: int) -> None:
    body = json.dumps({"error": {"message": "deployment is over capacity"}})
    invoker = FoundryDeploymentInvoker(
        _profile(),
        FakeCredential(),
        FakeTransport(status=status, body=body),
    )

    with pytest.raises(ProviderRejectedError) as error:
        invoker.complete(messages=_messages())

    assert error.value.status == status
    assert "over capacity" in str(error.value)
    assert SECRET not in str(error.value)


def test_transport_and_credential_failures_fail_closed() -> None:
    unreachable = FoundryDeploymentInvoker(
        _profile(),
        FakeCredential(),
        FakeTransport(error=OSError(f"tls handshake failed: {SECRET}")),
    )
    denied = FoundryDeploymentInvoker(
        _profile(), FakeCredential(error=RuntimeError(SECRET)), FakeTransport()
    )
    empty_token = FoundryDeploymentInvoker(
        _profile(), FakeCredential(token=""), FakeTransport()
    )

    with pytest.raises(ProviderUnreachableError) as unreachable_error:
        unreachable.complete(messages=_messages())
    with pytest.raises(ProviderRejectedError) as denied_error:
        denied.complete(messages=_messages())
    with pytest.raises(ProviderRejectedError):
        empty_token.complete(messages=_messages())

    assert SECRET not in str(unreachable_error.value)
    assert SECRET not in str(denied_error.value)


def test_preflight_composes_the_existing_snapshot() -> None:
    credential = FakeCredential()
    factory = FakeClientFactory()
    invoker = FoundryDeploymentInvoker(
        _profile(), credential, FakeTransport(), client_factory=factory
    )

    snapshot = invoker.preflight()
    direct = FoundryProvider(_profile(), credential, factory).preflight()

    assert snapshot == direct
    assert snapshot.auth.available
    assert snapshot.auth.authenticated
    assert snapshot.auth.reason == "ready"
    assert snapshot.models[0].model_deployment == "reasoning-model"
    assert SECRET not in str(asdict(snapshot))
    with pytest.raises(RuntimeError, match="client factory"):
        FoundryDeploymentInvoker(_profile(), credential, FakeTransport()).preflight()


def test_invocation_ledger_records_complete_redacted_records() -> None:
    ledger = InMemoryInvocationLedger(lambda: FIXED_NOW)
    request = {
        "model": "reasoning-model",
        "messages": [{"role": "user", "content": "analyze the strategy"}],
    }

    record = ledger.record(
        provider="azure_ai_foundry",
        model="reasoning-model",
        version="1.0.0",
        role="research",
        request_digest=content_digest(request),
        response_digest=content_digest(RESPONSE),
    )

    assert record.created_at == FIXED_NOW
    assert record.provider == "azure_ai_foundry"
    assert record.model == "reasoning-model"
    assert record.version == "1.0.0"
    assert record.role == "research"
    assert record.request_digest == content_digest(request)
    assert len(content_digest(request)) == 64
    portable = record.as_dict()
    assert portable["provider"] == "azure_ai_foundry"
    assert portable["requestDigest"] == record.request_digest
    assert portable["responseDigest"] == record.response_digest
    assert SECRET not in repr(record)
    assert SECRET not in str(portable)
    snapshot = _snapshot()
    assert SECRET not in str(asdict(snapshot))


def _snapshot() -> ProviderCapabilitySnapshot:
    return FoundryProvider(
        _profile(),
        FakeCredential(),
        FakeClientFactory(),
    ).preflight()


def test_role_binding_is_deterministic_for_foundry_profiles() -> None:
    profile = _profile()

    first = bind_role(
        "coding", profile, provider="azure_ai_foundry", clock=lambda: FIXED_NOW
    )
    second = bind_role(
        "coding", profile, provider="azure_ai_foundry", clock=lambda: FIXED_NOW
    )
    later = bind_role(
        "coding",
        profile,
        provider="azure_ai_foundry",
        clock=lambda: datetime(2026, 2, 2, tzinfo=UTC),
    )

    assert first == second
    assert first.assignment_digest == second.assignment_digest
    assert first.role == "coding"
    assert first.provider == "azure_ai_foundry"
    assert first.profile_id == "developer"
    assert first.model == "reasoning-model"
    assert first.assigned_at == FIXED_NOW
    assert later.assignment_digest != first.assignment_digest
    assert SECRET not in repr(first)
    assert SECRET not in str(first.as_dict())
    with pytest.raises(ValueError, match="provider must be resolvable"):
        bind_role("coding", profile, clock=lambda: FIXED_NOW)
