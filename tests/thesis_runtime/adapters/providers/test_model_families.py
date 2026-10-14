"""Family tool presentation through real provider exchange and durable sessions."""

import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator
from tests.thesis_runtime.provider_fakes import stream_response

from thesis_runtime.adapters.providers.families import model_family_adapter
from thesis_runtime.adapters.providers.openrouter import (
    OpenRouterProfile,
    OpenRouterProvider,
)
from thesis_runtime.adapters.providers.session_adapter import NativeSessionAdapter
from thesis_runtime.adapters.providers.transports import HttpOpenRouterTransport
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    decode_transcript,
    from_record,
)
from thesis_runtime.contracts.records import AgentInvocation, AgentInvocationKind
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.tools.registry import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)


def branch(name: str, field: str = "idea") -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["workflow", "input"],
        "additionalProperties": False,
        "properties": {
            "workflow": {"const": name},
            "input": {
                "type": "object",
                "properties": {field: {"type": "string", "minLength": 1}},
                "required": [field],
                "additionalProperties": False,
            },
        },
    }


class Key:
    def key(self) -> str:
        return "test-key"


@pytest.mark.parametrize("multiple", [False, True])
def test_deepseek_launch_exchange_preserves_validation_audit_and_replay(
    multiple: bool,
) -> None:
    schema = {
        "oneOf": [branch("research")]
        + ([branch("review", "report")] if multiple else [])
    }
    original = json.dumps(schema, sort_keys=True)
    arguments = {"workflow": "research", "input": {"idea": "Momentum"}}
    requests: list[dict[str, Any]] = []
    dispatched: list[Mapping[str, object]] = []

    def exchange(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            advertised = payload["tools"][0]["function"]["parameters"]
            # Reproduce the endpoint's root-property type lookup. The old union-only
            # presentation stringified input; the family presentation must not.
            supplied = dict(arguments)
            if (
                advertised.get("properties", {}).get("input", {}).get("type")
                != "object"
            ):
                supplied["input"] = json.dumps(supplied["input"])
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "launch-call",
                        "type": "function",
                        "function": {
                            "name": "start_workflow",
                            "arguments": json.dumps(supplied),
                        },
                    }
                ],
            }
        else:
            message = {"content": "Started"}
        return stream_response(
            200,
            json={
                "model": "deepseek/test",
                "provider": "Parasail",
                "id": f"generation-{len(requests)}",
                "choices": [{"message": message}],
            },
        )

    profile = OpenRouterProfile(profile_id="test", model="deepseek/test")
    provider = OpenRouterProvider(
        profile,
        Key(),
        HttpOpenRouterTransport(
            profile.base_url,
            client=httpx.Client(transport=httpx.MockTransport(exchange)),
        ),
    )
    adapter = NativeSessionAdapter(
        provider, family=model_family_adapter(provider.model)
    )
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Launch", owner_actor_id="operator")
    chat.send_message(
        "workspace", thread.chat_id, actor_id="operator", body="Start research"
    )

    def launch(args: Mapping[str, object], context: ToolContext) -> ToolExecution:
        assert context.workspace_id == "workspace"
        assert context.chat_id == thread.chat_id
        dispatched.append(args)
        return ToolExecution(ok=True, structured={"execution_id": "execution-1"})

    registry = SessionToolRegistry(
        [RegisteredTool({"name": "start_workflow", "parameters": schema}, launch)]
    )
    service = Conversations(
        chat=chat,
        provider=adapter,
        registry_factory=lambda _: registry,
        root_system_prompt="Policy",
    )
    assert service.root("workspace", thread.chat_id).run_turn() is not None
    assert dispatched == [arguments]
    assert json.dumps(schema, sort_keys=True) == original
    advertised = requests[0]["tools"][0]["function"]["parameters"]
    # Branch coupling, required fields and extra-property rejection remain exact.
    candidates = [
        arguments,
        {"workflow": "review", "input": {"report": "Report"}},
        {"workflow": "review", "input": {"idea": "wrong branch"}},
        {"workflow": "research", "input": "{}"},
        {"workflow": "research", "input": {"idea": ""}},
        {"workflow": "research", "input": {"idea": "x", "extra": 1}},
        {},
    ]
    for candidate in candidates:
        assert Draft202012Validator(advertised).is_valid(
            candidate
        ) == Draft202012Validator(schema).is_valid(candidate)
    durable = chat.list_messages("workspace", thread.chat_id)
    output = from_record(durable[1])
    assert output.content.requests[0].name == "start_workflow"
    metadata = json.loads(output.content.as_content())["result"]["provider_metadata"]
    assert metadata["upstream_provider"] == "Parasail"
    assert metadata["generation_id"] == "generation-1"
    replay = (
        SessionMessage(
            "policy", "instruction", MessageContent("Policy", "instruction")
        ),
        *decode_transcript(durable[:-1]),
    )
    snapshot = adapter.request_snapshot(messages=replay, tools=registry.catalog())
    assert snapshot["messages"] == requests[1]["messages"]
    assert snapshot["tools"] == requests[1]["tools"]
    assert snapshot["model_family"] == "deepseek"
    invocations = chat.persistence.list_conversation(
        AgentInvocation, "workspace", thread.chat_id
    )
    audits = [i for i in invocations if i.kind is AgentInvocationKind.MODEL]
    audit = audits[0].as_dict()["provider_request"]
    assert isinstance(audit, dict)
    assert audit["tools"] == requests[0]["tools"]
    assert audit["family_adapter_version"] == "1"


def test_other_families_keep_native_tool_schema_and_raw_arguments() -> None:
    schema = {"oneOf": [branch("research")]}

    def exchange(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["tools"][0]["function"]["parameters"] == schema
        return stream_response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call",
                                    "function": {
                                        "name": "start",
                                        "arguments": "malformed JSON",
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    profile = OpenRouterProfile(profile_id="test", model="other/model")
    provider = OpenRouterProvider(
        profile,
        Key(),
        HttpOpenRouterTransport(
            profile.base_url,
            client=httpx.Client(transport=httpx.MockTransport(exchange)),
        ),
    )
    adapter = NativeSessionAdapter(
        provider, family=model_family_adapter(provider.model)
    )
    output = adapter.complete(
        messages=[SessionMessage("input", "incoming", MessageContent("Start", "user"))],
        tools=[{"name": "start", "parameters": schema}],
    )
    assert output.content.requests[0].arguments == "malformed JSON"
