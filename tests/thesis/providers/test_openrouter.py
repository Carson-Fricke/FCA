from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from thesis.providers import (
    ChatMessage,
    InMemoryInvocationLedger,
    OpenRouterProfile,
    OpenRouterProvider,
    ProviderRejectedError,
    ProviderUnreachableError,
    ToolCall,
    bind_role,
    content_digest,
    messages_payload,
)

SECRET = "sk-or-secret-must-not-escape"
FIXED_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)

RESPONSE = json.dumps(
    {
        "id": "chatcmpl-1",
        "model": "z-ai/glm-5.3-flash",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "answer text"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }
)


class FakeKeySource:
    def __init__(self, secret: str = SECRET, error: Exception | None = None) -> None:
        self._secret = secret
        self.error = error
        self.calls = 0

    def key(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self._secret


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
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        self.calls.append((path, dict(headers), dict(payload)))
        if self.error is not None:
            raise self.error
        return self.status, self.body


def _profile(*, enabled: bool = True) -> OpenRouterProfile:
    return OpenRouterProfile(
        profile_id="openrouter-glm",
        model="z-ai/glm-5.3-flash",
        base_url="https://openrouter.ai/api/v1",
        enabled=enabled,
    )


def _messages() -> tuple[ChatMessage, ...]:
    return (
        ChatMessage(role="system", content="system prompt"),
        ChatMessage(role="user", content="user question"),
    )


def test_happy_path_sends_conventional_payload_and_headers() -> None:
    transport = FakeTransport()

    completion = OpenRouterProvider(_profile(), FakeKeySource(), transport).complete(
        messages=_messages(), role="coding"
    )

    path, headers, payload = transport.calls[0]
    assert path == "/chat/completions"
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert headers["HTTP-Referer"] == "https://thesis.local/fca"
    assert headers["X-Title"] == "fca-thesis"
    assert headers["Content-Type"] == "application/json"
    assert payload["model"] == "z-ai/glm-5.3-flash"
    assert payload["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user question"},
    ]
    assert completion.content == "answer text"
    assert completion.model == "z-ai/glm-5.3-flash"
    assert completion.provider == "openrouter"
    assert completion.role == "coding"
    assert completion.prompt_tokens == 11
    assert completion.completion_tokens == 7
    assert completion.schema_version == 1


def test_completion_record_and_provider_serialize_without_credentials() -> None:
    transport = FakeTransport()
    provider = OpenRouterProvider(_profile(), FakeKeySource(), transport)

    completion = provider.complete(messages=_messages())
    snapshot = provider.preflight()

    assert SECRET not in repr(provider)
    assert SECRET not in repr(completion)
    assert SECRET not in repr(snapshot)
    assert SECRET not in str(asdict(snapshot))
    assert SECRET not in json.dumps(dict(transport.calls[0][2]))
    assert SECRET in json.dumps(dict(transport.calls[0][1]))
    assert "Bearer" not in repr(provider)


def test_key_is_resolved_per_invocation_and_never_cached() -> None:
    key_source = FakeKeySource()
    provider = OpenRouterProvider(_profile(), key_source, FakeTransport())

    provider.complete(messages=_messages())
    provider.complete(messages=_messages())

    assert key_source.calls == 2
    stored = [getattr(provider, slot) for slot in provider.__slots__]
    assert SECRET not in repr(stored)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_non_2xx_responses_fail_closed_with_typed_error(status: int) -> None:
    body = json.dumps({"error": {"message": "quota exhausted"}})
    provider = OpenRouterProvider(
        _profile(), FakeKeySource(), FakeTransport(status=status, body=body)
    )

    with pytest.raises(ProviderRejectedError) as error:
        provider.complete(messages=_messages())

    assert error.value.status == status
    assert "quota exhausted" in str(error.value)
    assert SECRET not in str(error.value)


def test_transport_failure_is_fail_closed_as_unreachable() -> None:
    provider = OpenRouterProvider(
        _profile(),
        FakeKeySource(),
        FakeTransport(error=OSError(f"connection reset: {SECRET}")),
    )

    with pytest.raises(ProviderUnreachableError) as error:
        provider.complete(messages=_messages())

    assert SECRET not in str(error.value)


def test_missing_key_fails_closed_without_exposing_the_source_error() -> None:
    failing = OpenRouterProvider(
        _profile(), FakeKeySource(error=RuntimeError(SECRET)), FakeTransport()
    )
    empty = OpenRouterProvider(_profile(), FakeKeySource(secret=""), FakeTransport())

    with pytest.raises(ProviderRejectedError) as failure:
        failing.complete(messages=_messages())
    with pytest.raises(ProviderRejectedError):
        empty.complete(messages=_messages())

    assert SECRET not in str(failure.value)


def test_preflight_records_status_without_exposing_the_key() -> None:
    ready = OpenRouterProvider(_profile(), FakeKeySource(), FakeTransport()).preflight()
    rejected = OpenRouterProvider(
        _profile(), FakeKeySource(), FakeTransport(status=401, body="denied")
    ).preflight()
    unreachable = OpenRouterProvider(
        _profile(), FakeKeySource(), FakeTransport(error=OSError("down"))
    ).preflight()
    unauthenticated = OpenRouterProvider(
        _profile(), FakeKeySource(secret=""), FakeTransport()
    ).preflight()
    disabled = OpenRouterProvider(
        _profile(enabled=False), FakeKeySource(), FakeTransport()
    ).preflight()

    assert ready.auth.available and ready.auth.authenticated
    assert ready.auth.reason == "ready"
    assert ready.models[0].model_deployment == "z-ai/glm-5.3-flash"
    assert rejected.auth.reason == "authentication_unavailable"
    assert rejected.auth.authenticated
    assert unreachable.auth.reason == "provider_unreachable"
    assert not unreachable.auth.available
    assert unauthenticated.auth.reason == "authentication_unavailable"
    assert not unauthenticated.auth.authenticated
    assert disabled.auth.reason == "not_configured"
    assert SECRET not in str(asdict(ready))


def test_invocation_record_is_complete_and_redacted() -> None:
    ledger = InMemoryInvocationLedger(lambda: FIXED_NOW)
    request = {"model": "z-ai/glm-5.3-flash", "messages": ["user question"]}

    record = ledger.record(
        provider="openrouter",
        model="z-ai/glm-5.3-flash",
        version="1.0.0",
        role="primary",
        request_digest=content_digest(request),
        response_digest=content_digest(RESPONSE),
    )
    critique = ledger.record(
        provider="openrouter",
        model="z-ai/glm-5.3-flash",
        version="1.0.0",
        role="critique",
        request_digest=content_digest({"second": "request"}),
        response_digest=content_digest(RESPONSE),
    )

    assert record.created_at == FIXED_NOW
    assert record.provider == "openrouter"
    assert record.model == "z-ai/glm-5.3-flash"
    assert record.version == "1.0.0"
    assert record.role == "primary"
    assert record.request_digest == content_digest(request)
    assert record.response_digest == content_digest(RESPONSE)
    assert record.schema_version == 1
    assert ledger.list() == (record, critique)
    portable = record.as_dict()
    assert set(portable) == {
        "provider",
        "model",
        "version",
        "role",
        "requestDigest",
        "responseDigest",
        "createdAt",
        "schemaVersion",
    }
    assert portable["createdAt"] == "2026-01-15T12:00:00Z"
    assert SECRET not in repr(record)
    assert SECRET not in str(portable)
    with pytest.raises(ValueError, match="SHA-256"):
        ledger.record(
            provider="openrouter",
            model="z-ai/glm-5.3-flash",
            version="1.0.0",
            role="primary",
            request_digest="not-a-digest",
            response_digest=content_digest(RESPONSE),
        )


def test_role_binding_is_deterministic_and_pre_result() -> None:
    profile = _profile()

    first = bind_role("primary", profile, clock=lambda: FIXED_NOW)
    second = bind_role("primary", profile, clock=lambda: FIXED_NOW)
    later = bind_role(
        "primary", profile, clock=lambda: datetime(2026, 1, 16, tzinfo=UTC)
    )

    assert first == second
    assert first.assignment_digest == second.assignment_digest
    assert first.assigned_at == FIXED_NOW
    assert first.provider == "openrouter"
    assert first.model == "z-ai/glm-5.3-flash"
    assert first.profile_id == "openrouter-glm"
    assert later.assignment_digest != first.assignment_digest
    assert later.assigned_at != FIXED_NOW
    serialized = first.as_dict()
    assert serialized["role"] == "primary"
    assert serialized["assignmentDigest"] == first.assignment_digest
    assert SECRET not in repr(first)
    assert SECRET not in str(serialized)
    with pytest.raises(ValueError, match="unsupported Thesis role"):
        bind_role("not-a-role", profile, clock=lambda: FIXED_NOW)


def test_openrouter_profile_rejects_non_https_base_urls() -> None:
    with pytest.raises(ValueError, match="HTTPS service URL"):
        OpenRouterProfile(
            profile_id="openrouter-glm",
            model="z-ai/glm-5.3-flash",
            base_url="http://openrouter.ai/api/v1",
        )
    with pytest.raises(ValueError, match="portable identifier"):
        OpenRouterProfile(profile_id="openrouter-glm", model="")


def _tool_call_message(
    *,
    content: object = None,
    tool_calls: object = None,
    extra: Mapping[str, object] | None = None,
) -> str:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if extra:
        message.update(extra)
    return json.dumps(
        {
            "model": "z-ai/glm-5.3-flash",
            "choices": [
                {"index": 0, "message": message, "finish_reason": "tool_calls"}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }
    )


def test_complete_parses_tool_calls_with_reasoning_on_a_separate_channel() -> None:
    body = _tool_call_message(
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "run_backtest",
                    "arguments": '{"config_path": "cfg.yaml"}',
                },
            }
        ],
        extra={
            "reasoning": "SECRET-CHAIN-OF-THOUGHT",
            "reasoning_content": "SECRET-REASONING",
        },
    )
    transport = FakeTransport(body=body)
    provider = OpenRouterProvider(_profile(), FakeKeySource(), transport)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "run_backtest",
                "description": "run a backtest",
                "parameters": {"type": "object"},
            },
        }
    ]

    completion = provider.complete(messages=_messages(), tools=tools)

    assert transport.calls[0][2]["tools"] == tools
    assert completion.content == ""
    assert completion.reasoning == "SECRET-CHAIN-OF-THOUGHT"
    assert "SECRET" not in completion.content
    assert completion.tool_calls == (
        ToolCall(
            id="call-1",
            name="run_backtest",
            arguments='{"config_path": "cfg.yaml"}',
        ),
    )


def test_complete_reads_reasoning_content_when_reasoning_absent() -> None:
    body = _tool_call_message(
        content="answer text", extra={"reasoning_content": "SECRET-REASONING"}
    )
    provider = OpenRouterProvider(_profile(), FakeKeySource(), FakeTransport(body=body))

    completion = provider.complete(messages=_messages())

    assert completion.content == "answer text"
    assert completion.reasoning == "SECRET-REASONING"


def test_complete_ignores_non_string_reasoning_without_failing() -> None:
    body = _tool_call_message(
        content="answer text",
        extra={"reasoning": ["not", "text"], "reasoning_content": 7},
    )
    provider = OpenRouterProvider(_profile(), FakeKeySource(), FakeTransport(body=body))

    completion = provider.complete(messages=_messages())

    assert completion.content == "answer text"
    assert completion.reasoning == ""


def test_complete_omits_tools_when_none_are_supplied() -> None:
    transport = FakeTransport()

    OpenRouterProvider(_profile(), FakeKeySource(), transport).complete(
        messages=_messages()
    )

    assert "tools" not in transport.calls[0][2]


def test_complete_forwards_response_format_when_supplied() -> None:
    transport = FakeTransport()

    OpenRouterProvider(_profile(), FakeKeySource(), transport).complete(
        messages=_messages(),
        response_format={"type": "json_object"},
    )

    assert transport.calls[0][2]["response_format"] == {"type": "json_object"}


def test_complete_omits_response_format_when_not_supplied() -> None:
    transport = FakeTransport()

    OpenRouterProvider(_profile(), FakeKeySource(), transport).complete(
        messages=_messages()
    )

    assert "response_format" not in transport.calls[0][2]


@pytest.mark.parametrize(
    "tool_call",
    [
        {"type": "function", "function": {"name": "x", "arguments": "{}"}},
        {"id": "call-1", "function": "not-an-object"},
        {"id": "call-1", "function": {"name": "x", "arguments": {"a": 1}}},
        {"id": "call-1", "function": {"name": "", "arguments": "{}"}},
    ],
)
def test_malformed_tool_calls_fail_closed(tool_call: object) -> None:
    body = _tool_call_message(content="", tool_calls=[tool_call])
    provider = OpenRouterProvider(_profile(), FakeKeySource(), FakeTransport(body=body))

    with pytest.raises(ProviderRejectedError):
        provider.complete(messages=_messages())


def test_null_content_without_tool_calls_fails_closed() -> None:
    body = _tool_call_message(extra={"reasoning": "only reasoning"})
    provider = OpenRouterProvider(_profile(), FakeKeySource(), FakeTransport(body=body))

    with pytest.raises(ProviderRejectedError):
        provider.complete(messages=_messages())


def test_tool_messages_serialize_with_call_ids() -> None:
    assistant = ChatMessage(
        role="assistant",
        content="",
        tool_calls=(ToolCall(id="call-1", name="get_run", arguments="{}"),),
    )
    result = ChatMessage(role="tool", content="ok", tool_call_id="call-1")

    wire = messages_payload((assistant, result))

    tool_calls = wire[0]["tool_calls"]
    assert isinstance(tool_calls, list)
    assert tool_calls[0]["function"] == {
        "name": "get_run",
        "arguments": "{}",
    }
    assert wire[1] == {"role": "tool", "content": "ok", "tool_call_id": "call-1"}
