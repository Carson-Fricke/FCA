"""Wiring coverage for the workflow runtime on the chat reply service.

The structured chat trigger and the plain-language research predicate can only
execute when the workflow runtime reaches
:class:`~thesis.chat.responder.ChatReplyService`.  These tests prove
``create_app`` attaches it both to the reply service it composes from a
``model_provider`` and to an explicitly injected reply service, while leaving
the service unconfigured when no runtime is supplied.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from fastapi.testclient import TestClient

from thesis.chat import ChatReplyService, ChatService, InMemoryChatStreamBroker
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


def test_create_app_attaches_runtime_to_composed_reply_service() -> None:
    runtime = _FakeRuntime()
    app = create_app(
        bearer_token=TOKEN,
        model_provider=_model_provider(),
        workflow_runtime=cast(WorkflowRuntime, runtime),
    )

    assert app.state.chat_reply_service.development_runtime is runtime


def test_create_app_attaches_runtime_to_injected_reply_service() -> None:
    runtime = _FakeRuntime()
    chat = ChatService()
    broker = InMemoryChatStreamBroker()
    reply = ChatReplyService(
        chat_service=chat,
        broker=broker,
        provider=lambda history: iter(()),
    )
    app = create_app(
        bearer_token=TOKEN,
        chat_service=chat,
        chat_stream_broker=broker,
        chat_reply_service=reply,
        workflow_runtime=cast(WorkflowRuntime, runtime),
    )

    assert app.state.chat_reply_service is reply
    assert reply.development_runtime is runtime


def test_create_app_without_runtime_leaves_reply_service_unconfigured() -> None:
    app = create_app(bearer_token=TOKEN, model_provider=_model_provider())

    assert app.state.chat_reply_service.development_runtime is None
    assert TestClient(app).get("/health").json() == {"status": "ok"}
