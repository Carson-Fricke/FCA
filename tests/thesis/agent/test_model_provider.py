"""Coverage for the workflow OpenRouter model-provider adapter.

The adapter is the bridge between the workflow ``ModelProvider`` boundary (a
rendered :class:`~thesis.agent.context.PromptContext`) and OpenRouter chat
completions.  These tests use a recording transport to prove the rendered
prompt becomes a ``system`` + ``user`` message pair, the completion content is
returned, and an empty content falls back to reasoning or fails closed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from thesis.agent.context import assemble_context
from thesis.agent.model_provider import OpenRouterModelProvider
from thesis.agent.router import ModelProfile
from thesis.providers.invocations import ProviderRejectedError
from thesis.providers.openrouter import OpenRouterProfile, OpenRouterProvider


class _KeySource:
    def key(self) -> str:
        return "sk-test-secret"


class _RecordingTransport:
    def __init__(self, body: str) -> None:
        self._body = body
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        self.calls.append(dict(payload))
        return 200, self._body


def _completion(*, content: str, reasoning: str = "") -> str:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning"] = reasoning
    return json.dumps(
        {
            "model": "test/model",
            "choices": [{"index": 0, "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )


def _provider(transport: _RecordingTransport) -> OpenRouterProvider:
    return OpenRouterProvider(
        OpenRouterProfile(profile_id="openrouter-test", model="test/model"),
        _KeySource(),
        transport,
    )


def _profile() -> ModelProfile:
    return ModelProfile(
        profile_id="local-openrouter",
        version="1.0.0",
        provider="openrouter",
        model="test/model",
    )


def test_invoke_maps_rendered_prompt_to_provider_messages() -> None:
    transport = _RecordingTransport(_completion(content="done"))
    adapter = OpenRouterModelProvider(_provider(transport), system_prompt="System.")
    prompt = assemble_context(
        workspace_scope="workspace-a", task="Analyze the claim.", role="research"
    )

    response = adapter.invoke(profile=_profile(), prompt=prompt, allowed_tools=())

    assert response.output == "done"
    messages = transport.calls[0]["messages"]
    assert messages == [
        {"role": "system", "content": "System."},
        {"role": "user", "content": prompt.rendered},
    ]


def test_invoke_falls_back_to_reasoning_when_content_is_empty() -> None:
    transport = _RecordingTransport(_completion(content="", reasoning="thinking"))
    adapter = OpenRouterModelProvider(_provider(transport))
    prompt = assemble_context(
        workspace_scope="workspace-a", task="Analyze.", role="research"
    )

    response = adapter.invoke(profile=_profile(), prompt=prompt, allowed_tools=())

    assert response.output == "thinking"


def test_invoke_fails_closed_when_completion_is_empty() -> None:
    transport = _RecordingTransport(_completion(content=""))
    adapter = OpenRouterModelProvider(_provider(transport))
    prompt = assemble_context(
        workspace_scope="workspace-a", task="Analyze.", role="research"
    )

    with pytest.raises(ProviderRejectedError, match="empty completion"):
        adapter.invoke(profile=_profile(), prompt=prompt, allowed_tools=())
