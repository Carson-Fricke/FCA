"""Wiring coverage for the workflow runtime on the HTTP application.

The workflow runtime is attached to the application for the workflow-launch
tool to resolve.  These tests prove ``create_app`` exposes it on the app state
both when it composes a conversations service from a ``model_provider`` and
when an explicitly injected service is supplied, while leaving the app
unconfigured when no runtime is supplied.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from fastapi.testclient import TestClient

from thesis.agent.runtime.conversation import Conversations
from thesis.chat import ChatService
from thesis.http.application import create_app
from thesis.providers.openrouter import OpenRouterProfile, OpenRouterProvider
from thesis.workflows.runtime import WorkflowRuntime

TOKEN = "thesis-test-token"


class _KeySource:
    def key(self) -> str:
        return "sk-test-secret"


class _NoopTransport:
    def post(
        self,
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        raise AssertionError("the model provider must not be called while wiring")


def _model_provider() -> OpenRouterProvider:
    return OpenRouterProvider(
        OpenRouterProfile(profile_id="openrouter-test", model="test/model"),
        _KeySource(),
        _NoopTransport(),
    )


class _FakeRuntime:
    """Marker object standing in for the workflow runtime."""


def test_create_app_attaches_runtime_to_the_app_state() -> None:
    runtime = _FakeRuntime()
    app = create_app(
        bearer_token=TOKEN,
        model_provider=_model_provider(),
        workflow_runtime=cast(WorkflowRuntime, runtime),
    )

    assert app.state.workflow_runtime is runtime
    assert isinstance(app.state.conversations, Conversations)


def test_create_app_exposes_an_injected_conversations_service() -> None:
    runtime = _FakeRuntime()
    chat = ChatService()
    conversations = Conversations(chat=chat)
    app = create_app(
        bearer_token=TOKEN,
        chat_service=chat,
        conversations=conversations,
        workflow_runtime=cast(WorkflowRuntime, runtime),
    )

    assert app.state.conversations is conversations
    assert app.state.workflow_runtime is runtime


def test_create_app_without_runtime_still_serves() -> None:
    app = create_app(bearer_token=TOKEN, model_provider=_model_provider())

    assert getattr(app.state, "workflow_runtime", None) is None
    assert isinstance(app.state.conversations, Conversations)
    assert TestClient(app).get("/health").json() == {"status": "ok"}
