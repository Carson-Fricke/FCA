"""Serving composition: chat-only inertness and workflow-runtime wiring."""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI

from thesis.agent.harness_tools import HarnessToolProvider
from thesis.chat.service import ChatService
from thesis.http import application, serving
from thesis.integrations.mcp import HarnessMcpClient
from thesis.providers.openrouter import OpenRouterProvider


def _stub_harness() -> HarnessToolProvider:
    return HarnessToolProvider(client=cast(HarnessMcpClient, object()))


def test_stack_without_a_model_provider_stays_chat_only() -> None:
    stack = serving.build_serving_stack(chat_service=ChatService())

    assert stack.conversations is None
    assert stack.workflow_runtime is None
    assert stack.harness_tool_provider is None
    assert stack.broker is not None


def test_stack_composes_conversations_and_the_workflow_runtime() -> None:
    harness = _stub_harness()

    stack = serving.build_serving_stack(
        chat_service=ChatService(),
        model_provider=cast(OpenRouterProvider, object()),
        harness_tools=harness,
    )

    assert stack.conversations is not None
    assert stack.workflow_runtime is not None
    assert stack.harness_tool_provider is harness


def test_open_serving_model_provider_requires_a_key(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert serving.open_serving_model_provider() is None

    monkeypatch.setenv("OPENROUTER_API_KEY", "model-key")
    assert serving.open_serving_model_provider() is not None


def test_open_serving_harness_tools_requires_a_url(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_MCP_URL", raising=False)
    assert serving.open_serving_harness_tools() is None


def _stub_serving_environment(monkeypatch, *, chat_service: ChatService) -> None:
    monkeypatch.setenv("THESIS_API__BEARER_TOKEN", "serving-token")
    monkeypatch.delenv("THESIS_API__ENTRA_TENANT_ID", raising=False)
    monkeypatch.delenv("THESIS_API__ENTRA_AUDIENCE", raising=False)
    monkeypatch.setattr(
        "thesis.cosmos.stack.open_cosmos_chat_stack", lambda: (chat_service, None)
    )
    monkeypatch.setattr("thesis.http.application.serving_workspace_store", lambda: None)
    monkeypatch.setattr(
        "thesis.http.workspaces.build_local_workspace_data",
        lambda **kwargs: None,
    )


def test_serving_app_from_environment_passes_the_workflow_runtime(
    monkeypatch,
) -> None:
    chat_service = ChatService()
    _stub_serving_environment(monkeypatch, chat_service=chat_service)
    model = cast(OpenRouterProvider, object())
    harness = _stub_harness()
    monkeypatch.setattr(
        "thesis.http.serving.open_serving_model_provider", lambda: model
    )
    monkeypatch.setattr(
        "thesis.http.serving.open_serving_harness_tools", lambda: harness
    )
    monkeypatch.setattr("thesis.cosmos.stack.open_execution_store", object)
    captured: dict[str, object] = {}

    def fake_create_app(**kwargs: object) -> FastAPI:
        captured.update(kwargs)
        return FastAPI()

    monkeypatch.setattr("thesis.http.application.create_app", fake_create_app)

    application.serving_app_from_environment()

    assert captured["workflow_runtime"] is not None
    assert captured["conversations"] is not None
    assert captured["harness_tool_provider"] is harness
    assert captured["chat_service"] is chat_service


def test_serving_app_from_environment_stays_chat_only_without_a_model_key(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _stub_serving_environment(monkeypatch, chat_service=ChatService())
    captured: dict[str, object] = {}

    def fake_create_app(**kwargs: object) -> FastAPI:
        captured.update(kwargs)
        return FastAPI()

    monkeypatch.setattr("thesis.http.application.create_app", fake_create_app)

    application.serving_app_from_environment()

    assert captured["workflow_runtime"] is None
    assert captured["conversations"] is None
