"""OpenRouter transport retries connection-level failures only."""

from __future__ import annotations

import ssl
from collections.abc import Mapping

import httpx
import pytest

from thesis_runtime.adapters.providers.foundry import (
    FoundryDeploymentInvoker,
    FoundryProviderProfile,
)
from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    ProviderRejectedError,
    ProviderUnreachableError,
)
from thesis_runtime.adapters.providers.openrouter import (
    OpenRouterProfile,
    OpenRouterProvider,
)
from thesis_runtime.adapters.providers.transports import HttpOpenRouterTransport

_OK_BODY = '{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
_MESSAGES = [ProviderMessage(role="user", content="hi")]


class _Key:
    def key(self) -> str:
        return "test-key"


class _ScriptedTransport:
    """Return or raise the scripted outcome for each call in order."""

    def __init__(self, outcomes: list[tuple[int, str] | Exception]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def stream(self, path, headers, payload):
        from tests.thesis_runtime.provider_fakes import completion_events

        yield from completion_events(*self.post(path, headers, payload))

    def post(
        self,
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _provider(
    transport: _ScriptedTransport | HttpOpenRouterTransport,
) -> OpenRouterProvider:
    return OpenRouterProvider(
        OpenRouterProfile(profile_id="test", model="test/model"),
        _Key(),
        transport,
    )


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "thesis_runtime.adapters.providers.openrouter.time.sleep", lambda _: None
    )


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
def test_transient_connection_error_is_retried(
    error_type: type[httpx.TransportError],
) -> None:
    transport = _ScriptedTransport([error_type("dropped"), (200, _OK_BODY)])
    completion = _provider(transport).complete(messages=_MESSAGES)
    assert isinstance(completion, ModelCompletion)
    assert completion.message.content == "ok"
    assert transport.calls == 2


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
def test_persistent_connection_error_reports_unreachable_after_bounded_retries(
    error_type: type[httpx.TransportError],
) -> None:
    error = error_type("secret transport diagnostic")
    transport = _ScriptedTransport([error])
    with pytest.raises(ProviderUnreachableError) as raised:
        _provider(transport).complete(messages=_MESSAGES)
    assert raised.value.__cause__ is error
    assert str(raised.value) == "openrouter transport is unreachable"
    assert transport.calls == 3


@pytest.mark.parametrize(
    "error",
    [
        ssl.SSLError("secret TLS diagnostic"),
        httpx.ReadTimeout("secret read diagnostic"),
        httpx.WriteTimeout("secret write diagnostic"),
        httpx.ReadError("secret read diagnostic"),
        httpx.WriteError("secret write diagnostic"),
        httpx.RemoteProtocolError("secret protocol diagnostic"),
        httpx.PoolTimeout("secret pool diagnostic"),
        ConnectionError("secret connection diagnostic"),
        OSError("secret socket diagnostic"),
    ],
)
def test_transport_failure_is_normalized_without_replay(error: Exception) -> None:
    transport = _ScriptedTransport([error, (200, _OK_BODY)])
    with pytest.raises(ProviderUnreachableError) as raised:
        _provider(transport).complete(messages=_MESSAGES)
    assert raised.value.__cause__ is error
    assert str(raised.value) == "openrouter transport is unreachable"
    assert transport.calls == 1


@pytest.mark.parametrize(
    "error",
    [ValueError("serialization bug"), TypeError("bad data"), RuntimeError("bug")],
)
def test_non_transport_failure_is_not_mislabeled_as_unreachable(
    error: Exception,
) -> None:
    transport = _ScriptedTransport([error])
    with pytest.raises(type(error)) as raised:
        _provider(transport).complete(messages=_MESSAGES)
    assert raised.value is error
    assert transport.calls == 1


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_http_rejection_is_unchanged_and_not_retried(status: int) -> None:
    transport = _ScriptedTransport([(status, '{"error":{"message":"rejected"}}')])
    with pytest.raises(ProviderRejectedError) as raised:
        _provider(transport).complete(messages=_MESSAGES)
    assert raised.value.status == status
    assert transport.calls == 1


@pytest.mark.parametrize(
    "error",
    [
        ssl.SSLError("secret TLS diagnostic"),
        httpx.ReadTimeout("secret read diagnostic"),
        httpx.RemoteProtocolError("secret protocol diagnostic"),
    ],
)
def test_concrete_http_transport_failure_is_normalized_without_replay(
    error: Exception,
) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise error

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = _provider(
            HttpOpenRouterTransport("https://example.test", client=client)
        )
        with pytest.raises(ProviderUnreachableError) as raised:
            provider.complete(messages=_MESSAGES)
    assert raised.value.__cause__ is error
    assert str(raised.value) == "openrouter transport is unreachable"
    assert calls == 1


@pytest.mark.parametrize(
    ("response_format", "error_type"),
    [({"invalid": object()}, TypeError), ({"invalid": float("nan")}, ValueError)],
)
def test_actual_payload_serialization_failure_is_not_a_network_failure(
    response_format: Mapping[str, object], error_type: type[Exception]
) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=_OK_BODY)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = _provider(
            HttpOpenRouterTransport("https://example.test", client=client)
        )
        with pytest.raises(error_type):
            provider.complete(messages=_MESSAGES, response_format=response_format)
    assert calls == 0


class _Token:
    token = "test-token"
    expires_on = 0


class _Credential:
    def get_token(self, *scopes: str) -> _Token:
        return _Token()


class _FoundryTransport:
    def __init__(self, outcomes: list[tuple[int, str] | Exception]) -> None:
        self._script = _ScriptedTransport(outcomes)

    @property
    def calls(self) -> int:
        return self._script.calls

    def stream(self, path, headers, payload):
        from tests.thesis_runtime.provider_fakes import completion_events

        yield from completion_events(*self.post(path, headers, payload))

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        return self._script.post(url, headers, payload)


def _foundry(transport: _FoundryTransport) -> FoundryDeploymentInvoker:
    return FoundryDeploymentInvoker(
        FoundryProviderProfile(
            profile_id="test",
            endpoint="https://example.test",
            project_name="test",
            model_deployment="test-model",
            credential_mode="default_azure_credential",
        ),
        _Credential(),
        transport,
    )


@pytest.mark.parametrize(
    "error",
    [
        ssl.SSLError("secret TLS diagnostic"),
        httpx.ReadTimeout("secret read diagnostic"),
        httpx.RemoteProtocolError("secret protocol diagnostic"),
        httpx.ConnectError("secret connect diagnostic"),
        OSError("secret socket diagnostic"),
    ],
)
def test_foundry_transport_failure_is_normalized_without_replay(
    error: Exception,
) -> None:
    transport = _FoundryTransport([error, (200, _OK_BODY)])
    with pytest.raises(ProviderUnreachableError) as raised:
        _foundry(transport).complete(messages=_MESSAGES)
    assert raised.value.__cause__ is error
    assert str(raised.value) == "foundry transport is unreachable"
    assert transport.calls == 1


@pytest.mark.parametrize(
    "error",
    [ValueError("serialization bug"), TypeError("bad data"), RuntimeError("bug")],
)
def test_foundry_programming_errors_propagate(error: Exception) -> None:
    transport = _FoundryTransport([error])
    with pytest.raises(type(error)) as raised:
        _foundry(transport).complete(messages=_MESSAGES)
    assert raised.value is error
    assert transport.calls == 1


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_foundry_http_rejection_is_unchanged(status: int) -> None:
    transport = _FoundryTransport([(status, '{"error":{"message":"rejected"}}')])
    with pytest.raises(ProviderRejectedError) as raised:
        _foundry(transport).complete(messages=_MESSAGES)
    assert raised.value.status == status
    assert transport.calls == 1
