"""Tests for the composed Thesis HTTP application boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fastapi.testclient import TestClient

from thesis.agent.harness_tools import (
    HARNESS_TOOL_NAMES,
    open_harness_mcp_composition,
    open_harness_tool_provider,
)
from thesis.http.application import create_app
from thesis.integrations.mcp import HarnessMcpTopology
from thesis.providers.openrouter import OpenRouterProfile, OpenRouterProvider
from thesis.providers.transports import DEFAULT_SYSTEM_PROMPT

TOKEN = "thesis-test-token"


def test_health_is_public_and_auth_check_requires_token() -> None:
    client = TestClient(create_app(bearer_token=TOKEN))

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/v1/auth/check").status_code == 401
    assert client.get(
        "/v1/auth/check", headers={"Authorization": f"Bearer {TOKEN}"}
    ).json() == {"status": "ok"}
    rejected = client.get(
        "/v1/auth/check", headers={"Authorization": "Bearer wrong-token"}
    )
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "unauthorized"
    assert "WWW-Authenticate" in rejected.headers


def test_api_fails_closed_without_configured_token(monkeypatch) -> None:
    monkeypatch.delenv("THESIS_API__BEARER_TOKEN", raising=False)
    client = TestClient(create_app())

    assert (
        client.get(
            "/v1/auth/check", headers={"Authorization": "Bearer anything"}
        ).status_code
        == 401
    )


def test_rejects_blank_configured_token() -> None:
    try:
        create_app(bearer_token="   ")
    except ValueError as error:
        assert "bearer token" in str(error)
    else:
        raise AssertionError("blank bearer token must be rejected")


def test_chat_surface_composed_with_injected_service(
    client: TestClient, launch
) -> None:
    chat = client.post(
        "/v1/workspaces/workspace-a/chats",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    launch(chat["chat_id"])

    listed = client.get(
        f"/v1/workspaces/workspace-a/chats/{chat['chat_id']}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()
    assert listed["chat_id"] == chat["chat_id"]
    assert listed["links"]["workflow_execution_ids"] == ["execution-001"]

    unauthenticated = TestClient(create_app(bearer_token=TOKEN))
    assert (
        unauthenticated.get(
            f"/v1/workspaces/workspace-a/chats/{chat['chat_id']}"
        ).status_code
        == 401
    )


class _KeySource:
    def key(self) -> str:
        return "sk-test-secret"


class _RecordingTransport:
    def __init__(self, body: str) -> None:
        self._body = body
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        self.calls.append(dict(payload))
        return 200, self._body


class _CatalogHandler:
    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        if message.get("method") == "tools/list":
            result: dict[str, object] = {
                "tools": [
                    {"name": name, "description": "", "inputSchema": {"type": "object"}}
                    for name in HARNESS_TOOL_NAMES
                ]
            }
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


def _completion_body(text: str) -> str:
    return json.dumps(
        {
            "model": "test/model",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )


def _model_provider(transport: _RecordingTransport) -> OpenRouterProvider:
    return OpenRouterProvider(
        OpenRouterProfile(profile_id="openrouter-test", model="test/model"),
        _KeySource(),
        transport,
    )


def _compose_reply(app, transport: _RecordingTransport) -> str:
    service = app.state.chat_service
    reply = app.state.chat_reply_service
    chat = service.create_chat(
        "workspace-a", title="Research", owner_actor_id="operator"
    )
    service.create_message(
        "workspace-a", chat.chat_id, actor_id="operator", body="hello"
    )
    history = service.list_messages("workspace-a", chat.chat_id)
    assert "".join(reply.provider(history)) == "Hi"
    return str(transport.calls[0]["messages"][0]["content"])


def test_create_app_composes_tool_aware_reply_service() -> None:
    transport = _RecordingTransport(_completion_body("Hi"))
    tools = open_harness_tool_provider(
        HarnessMcpTopology(thesis_location="local", harness_location="local"),
        in_process_handler=_CatalogHandler(),
    )

    app = create_app(
        bearer_token=TOKEN,
        model_provider=_model_provider(transport),
        harness_tool_provider=tools,
    )
    system = _compose_reply(app, transport)

    assert "tools" in transport.calls[0]
    assert system != DEFAULT_SYSTEM_PROMPT
    assert "harness" in system.lower()


def test_create_app_composes_plain_reply_service_without_tools() -> None:
    transport = _RecordingTransport(_completion_body("Hi"))

    app = create_app(bearer_token=TOKEN, model_provider=_model_provider(transport))
    system = _compose_reply(app, transport)

    assert "tools" not in transport.calls[0]
    assert system == DEFAULT_SYSTEM_PROMPT


def test_create_app_derives_the_tool_provider_from_a_shared_client() -> None:
    transport = _RecordingTransport(_completion_body("Hi"))
    composition = open_harness_mcp_composition(
        HarnessMcpTopology(thesis_location="local", harness_location="local"),
        in_process_handler=_CatalogHandler(),
    )

    app = create_app(
        bearer_token=TOKEN,
        model_provider=_model_provider(transport),
        harness_client=composition.client,
    )
    system = _compose_reply(app, transport)

    assert "tools" in transport.calls[0]
    assert system != DEFAULT_SYSTEM_PROMPT
    assert "harness" in system.lower()


def test_retired_validation_routes_are_absent() -> None:
    client = TestClient(create_app(bearer_token=TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}

    assert (
        client.get("/v1/workspaces/workspace-a/validation", headers=headers).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/workspaces/workspace-a/validation",
            headers=headers,
            json={"claim": "old claim"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/v1/workspaces/workspace-a/validation/val-1", headers=headers
        ).status_code
        == 404
    )
