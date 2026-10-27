"""HTTP compositions bind durable consumed-input ports to their one chat service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

import pytest
from fastapi.testclient import TestClient

from thesis.application.composition import build_serving_stack
from thesis.http.application import create_app
from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    Role,
)
from thesis_runtime.adapters.providers.openrouter import OpenRouterProvider
from thesis_runtime.conversations import ChatService


class Provider:
    model = "test/model"
    provider_name = "test"

    def stream(self, *, message_id=None, **kwargs):
        from tests.thesis_runtime.provider_fakes import completion_frames

        yield from completion_frames(self.complete(**kwargs), message_id)

    streaming = True

    def complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
        role: Role,
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ModelCompletion:
        return ModelCompletion(
            message=ProviderMessage("assistant", "Acknowledged"),
            model="test",
            provider="test",
            role=role,
            prompt_tokens=0,
            completion_tokens=0,
        )


@pytest.mark.parametrize("composition", ["direct", "serving"])
def test_both_compositions_acknowledge_root_and_api_intake(
    composition: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatService()
    provider = cast(OpenRouterProvider, Provider())
    if composition == "direct":
        app = create_app(
            bearer_token="token", chat_service=service, model_provider=provider
        )
    else:
        stack = build_serving_stack(chat_service=service, model_provider=provider)
        app = create_app(
            bearer_token="token",
            chat_service=service,
            conversations=stack.conversations,
        )
    conversations = app.state.conversations
    assert conversations._workers._read_consumed_input == service.read_consumed_input
    assert conversations._workers._acknowledge_input == service.acknowledge_input
    # Execute the worker synchronously so assertions observe its durable acknowledgement.
    monkeypatch.setattr(conversations._workers, "_spawn_thread", lambda work: work())
    chat = service.create_chat("workspace", title="Inputs", owner_actor_id="operator")
    client = TestClient(app)
    for prefix in ("", "/api"):
        response = client.post(
            prefix + f"/v1/workspaces/workspace/chats/{chat.chat_id}/messages",
            headers={"Authorization": "Bearer token"},
            json={"actor_id": "operator", "body": "Input"},
        )
        assert response.status_code == 200
        position = response.json()["input_position"]
        assert position > 0
        assert service.read_consumed_input("workspace", chat.chat_id, "") == position
        assert app.state.chat_service is service
        assert conversations._workers._states == {}
        # A replay after idle retirement reloads the durable fence without another turn.
        count = len(service.list_messages("workspace", chat.chat_id))
        conversations.schedule_input(
            service.get_message("workspace", response.json()["message_id"])
        )
        assert len(service.list_messages("workspace", chat.chat_id)) == count
        assert conversations._workers._states == {}
        current = client.get(
            prefix + f"/v1/workspaces/workspace/chats/{chat.chat_id}",
            headers={"Authorization": "Bearer token"},
        ).json()
        assert current["input_consumed_positions"] == {"": position}
    reloaded = ChatService(persistence=service.persistence)
    assert reloaded.read_consumed_input("workspace", chat.chat_id, "") == position
