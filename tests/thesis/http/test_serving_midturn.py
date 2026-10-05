"""Root serving intake retains its baseline and drains only fresh root input."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast

import pytest

from thesis.agent.runtime.session import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis.chat import ChatService
from thesis.chat.contracts import ChatMessage, MessageRole
from thesis.http import serving
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ModelCompletion, Role, ToolCall
from thesis.providers.openrouter import OpenRouterProvider


def test_root_full_baseline_and_midturn_send_drain_only_fresh_root_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Mid-turn", owner_actor_id="operator")
    for index in range(45):
        chat.send_message(
            "workspace", thread.chat_id, actor_id="operator", body=f"Old input {index}"
        )
    chat.send_message(
        "workspace",
        thread.chat_id,
        actor_id="operator",
        body="Existing scoped input",
        data={"subAgentId": "phase-agent"},
    )
    chat.send_message(
        "workspace", thread.chat_id, actor_id="operator", body="Newest initial input"
    )
    calls = (
        ToolCall("first-call", "pause", "{}"),
        ToolCall("second-call", "pause", "{}"),
    )
    requests: list[tuple[ProviderChatMessage, ...]] = []

    class Provider:
        def complete(
            self,
            *,
            messages: Sequence[ProviderChatMessage],
            role: Role,
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            requests.append(tuple(messages))
            index = len(requests) - 1
            return ModelCompletion(
                content="Finished" if index == 2 else "Checking",
                model="test-model",
                provider="test-provider",
                role=role,
                prompt_tokens=0,
                completion_tokens=0,
                tool_calls=() if index == 2 else (calls[index],),
            )

    stack = serving.build_serving_stack(
        chat_service=chat, model_provider=cast("OpenRouterProvider", Provider())
    )
    assert stack.conversations is not None
    root = stack.conversations.root("workspace", thread.chat_id)
    # Run synchronously while exercising send's durable write and wake path.
    monkeypatch.setattr(
        stack.conversations._workers, "_spawn_thread", lambda work: None
    )
    dispatched: list[Mapping[str, object]] = []

    def pause(arguments: Mapping[str, object]) -> ToolExecution:
        dispatched.append(arguments)
        if len(dispatched) == 1:
            root.send("Fresh root steering", actor_id="operator")
            chat.send_message(
                "workspace",
                thread.chat_id,
                actor_id="operator",
                body="Fresh scoped steering",
                data={"subAgentId": "phase-agent"},
            )
            chat.send_message(
                "workspace",
                thread.chat_id,
                actor_id="runtime",
                body="Fresh system notification",
                role=MessageRole.SYSTEM,
            )
        return ToolExecution(ok=True, text="Paused")

    registry = SessionToolRegistry(
        [
            RegisteredTool(
                {
                    "type": "function",
                    "function": {"name": "pause", "parameters": {"type": "object"}},
                },
                pause,
            )
        ]
    )
    contexts = []

    def registry_for(**kwargs: object) -> SessionToolRegistry:
        contexts.append(kwargs["context"])
        return registry

    monkeypatch.setattr(serving, "build_session_registry", registry_for)
    result = root.run_turn()
    assert isinstance(result, ChatMessage)
    assert result.body == "Finished"
    assert len(requests) == 3
    initial = [
        json.loads(message.content)["text"]
        for message in requests[0]
        if message.role == "user"
    ]
    assert initial == [f"Old input {index}" for index in range(45)] + [
        "Newest initial input"
    ]
    assert requests[1][-1].role == "user"
    assert json.loads(requests[1][-1].content) == {
        "source": "runtime",
        "text": "Fresh system notification",
        "correlation": None,
        "result": None,
    }
    assert requests[1][-3].tool_call_id == calls[0].id
    for request in requests:
        content = [
            json.loads(message.content)["text"]
            for message in request
            if message.role == "user"
        ]
        assert "Existing scoped input" not in content
        assert "Fresh scoped steering" not in content
        assert content.count("Old input 0") == 1
        assert content.count("Newest initial input") == 1
    assert all(
        sum(
            message.role == "user"
            and json.loads(message.content)["text"] == "Fresh root steering"
            for message in request
        )
        == 1
        for request in requests[1:]
    )
    assert all(
        sum(
            message.role == "user"
            and json.loads(message.content)["text"] == "Fresh system notification"
            for message in request
        )
        == 1
        for request in requests[1:]
    )
    assert requests[2][-1].tool_call_id == calls[1].id
    assert dispatched == [{}, {}]
    # Pending intake refreshes each model boundary without changing ownership.
    assert len(contexts) == len(requests)
    from thesis.agent.runtime.context import ToolContext

    assert isinstance(contexts[0], ToolContext)
    assert contexts[0].conversation_id == thread.chat_id
    assert contexts[0].allowed_tools == tuple(
        sorted({*serving.HARNESS_TOOL_NAMES, *serving.THESIS_TOOL_NAMES})
    )
    assert all(context == contexts[0] for context in contexts)
    assert contexts[0].intent_epoch == 0
