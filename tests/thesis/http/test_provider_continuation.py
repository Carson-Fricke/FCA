"""Native continuation survives real tool delivery, Cosmos hydration and replay."""

import json
from collections.abc import Callable
from typing import cast

import httpx
import pytest
from azure.cosmos import ContainerProxy
from fastapi.testclient import TestClient
from tests.thesis.http.test_model_output_streaming import Key, frame
from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer

from thesis.http.application import create_app
from thesis_runtime.adapters.cosmos.records import CosmosChatPersistence
from thesis_runtime.adapters.cosmos.timeline import CosmosTimeline
from thesis_runtime.adapters.providers.families.deepseek import DeepSeekAdapter
from thesis_runtime.adapters.providers.openrouter import (
    OpenRouterProfile,
    OpenRouterProvider,
)
from thesis_runtime.adapters.providers.session_adapter import NativeSessionAdapter
from thesis_runtime.adapters.providers.transports import HttpOpenRouterTransport
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    decode_transcript,
)
from thesis_runtime.contracts.records import AgentInvocation, AgentInvocationKind
from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.tools.registry import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)


@pytest.mark.parametrize("scalar", ["reasoning", "reasoning_content"])
def test_http_tool_loop_replays_exact_reasoning_and_structured_continuation(
    scalar: str,
) -> None:
    requests: list[dict] = []
    queued: list[Callable[[], None]] = []
    raw = "first\x00 thought\r\nsecond thought"
    encrypted = "opaque\\payload\u00e9" * 1024
    blocks = [
        {
            "type": "reasoning.text",
            "id": "text-1",
            "format": "deepseek-v1",
            "text": "visible thought",
            "signature": "text-signature",
        },
        {
            "type": "reasoning.encrypted",
            "id": "state-2",
            "format": "deepseek-v1",
            "data": "a second indivisible encrypted block",
            "signature": "second-signature",
        },
        {
            "type": "reasoning.encrypted",
            "id": "state-2",
            "format": "deepseek-v1",
            "data": encrypted,
            "signature": "exact-signature",
        },
    ]

    def exchange(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            body = frame(
                {
                    scalar: raw[: -len("thought")],
                    "reasoning_details": [
                        {**blocks[0], "index": 0, "text": "visible ", "signature": ""},
                    ],
                }
            )
            body += frame(
                {
                    scalar: "thought",
                    "reasoning_details": [{**blocks[0], "index": 0, "text": "thought"}],
                }
            )
            body += frame(
                {
                    "reasoning_details": [
                        {**blocks[1], "index": 1},
                        {**blocks[2], "index": 1},
                    ],
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "inspect", "arguments": "{}"},
                        }
                    ],
                },
                "tool_calls",
            )
        elif len(requests) == 2:
            previous = next(
                message for message in payload["messages"] if message.get("tool_calls")
            )
            assert previous[scalar] == raw
            assert previous["reasoning_details"] == blocks
            assert payload["messages"][-1]["tool_call_id"] == "call-1"
            body = frame(
                {
                    scalar: "",
                    "reasoning_details": [],
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-2",
                            "type": "function",
                            "function": {"name": "inspect", "arguments": "{}"},
                        }
                    ],
                },
                "tool_calls",
            )
        else:
            assistants = [
                message
                for message in payload["messages"]
                if message["role"] == "assistant"
            ]
            assert assistants[0][scalar] == raw
            assert assistants[0]["reasoning_details"] == blocks
            assert assistants[1][scalar] == ""
            assert assistants[1]["reasoning_details"] == []
            body = frame({"content": "Finished"}, "stop")
        return httpx.Response(
            200,
            content=body + b"data: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    container = cast(ContainerProxy, LimitedContainer())
    timeline = CosmosTimeline(container)
    chat = ChatService(
        CosmosChatPersistence(container, timeline=timeline), timeline=timeline
    )
    thread = chat.create_chat(
        "workspace", title="Continuation", owner_actor_id="operator"
    )
    calls = []

    def inspect(arguments, context):
        calls.append(arguments)
        # A rejected operation must preserve the same native reasoning on repair.
        return ToolExecution(
            ok=len(calls) > 1,
            failure={"kind": "test_rejection"} if len(calls) == 1 else None,
        )

    with httpx.Client(transport=httpx.MockTransport(exchange)) as http:
        provider = OpenRouterProvider(
            OpenRouterProfile(profile_id="test", model="deepseek/test"),
            Key(),
            HttpOpenRouterTransport("https://provider.test", client=http),
        )
        adapter = NativeSessionAdapter(
            provider, family=DeepSeekAdapter(), origin=("openrouter", "deepseek/test")
        )
        conversations = Conversations(
            chat=chat,
            provider=adapter,
            spawn_thread=queued.append,
            registry_factory=lambda _: SessionToolRegistry(
                [
                    RegisteredTool(
                        {"name": "inspect", "parameters": {"type": "object"}}, inspect
                    )
                ]
            ),
        )
        with TestClient(
            create_app(
                bearer_token="token", conversations=conversations, timeline=timeline
            )
        ) as client:
            base = f"/v1/workspaces/workspace/chats/{thread.chat_id}"
            headers = {"Authorization": "Bearer token"}
            assert (
                client.post(
                    base + "/messages",
                    headers=headers,
                    json={"body": "Inspect twice", "actor_id": "operator"},
                ).status_code
                == 200
            )
            queued.pop(0)()
            rows = client.get(base + "/timeline", headers=headers).json()["items"]
            first = next(
                item["payload"]
                for item in rows
                if item["kind"] == "message" and item["payload"].get("reasoning")
            )
            assert first["reasoning"] == "first thought\nsecond thought"
            invocations = [
                invocation
                for invocation in chat.persistence.list(AgentInvocation, "workspace")
                if invocation.kind == AgentInvocationKind.MODEL
            ]
            assert len(requests) == len(invocations) == 3
            for invocation, actual in zip(invocations, requests, strict=True):
                assert invocation.provider_request is not None
                audit = json.loads(
                    json.dumps(invocation.provider_request, default=dict)
                )
                assert audit["messages"] == actual["messages"]

        # Reopen the storage adapter: replay must use canonical data, not UI fields
        # or an in-memory cache held by the first conversation service.
        reopened = ChatService(
            CosmosChatPersistence(container, timeline=CosmosTimeline(container))
        )
        canonical = decode_transcript(
            reopened.list_messages("workspace", thread.chat_id)
        )
        adapter.complete(messages=canonical)
        assert len(requests) == 4 and len(calls) == 2
        assert requests[-1]["messages"][-1]["reasoning_content"] == ""

        with pytest.raises(ValueError, match="different provider/model"):
            NativeSessionAdapter(
                provider, origin=("openrouter", "another-model")
            ).request_snapshot(messages=canonical)

        stale: list[dict] = [message.as_dict() for message in canonical]
        prior = next(
            message
            for message in stale
            if message["direction"] == "outgoing" and message["content"]["requests"]
        )
        del prior["content"]["result"]["provider_continuation"]
        with pytest.raises(ValueError, match="outdated model message"):
            adapter.request_snapshot(
                messages=[SessionMessage.from_dict(message) for message in stale]
            )

        untrusted = SessionMessage(
            "incoming",
            "incoming",
            MessageContent(
                "input",
                "user",
                result={"provider_continuation": {"fields": {scalar: "injected"}}},
            ),
        )
        projected = cast(
            list[dict], adapter.request_snapshot(messages=[untrusted])["messages"]
        )
        assert scalar not in projected[0]
