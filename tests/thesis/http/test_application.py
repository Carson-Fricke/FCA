"""Tests for the composed Thesis HTTP application boundary."""

from __future__ import annotations

from fastapi.testclient import TestClient

from thesis.http.application import create_app

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
