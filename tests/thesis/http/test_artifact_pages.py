"""Agent artifact windows through HTTP intake, the model loop and harness MCP."""

import json

import pytest
from fastapi.testclient import TestClient

from thesis.agent.harness_tools import open_harness_tool_provider
from thesis.extensions.tools.registry import build_session_registry
from thesis.http.application import create_app
from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    ToolCall,
)
from thesis_runtime.adapters.providers.session_adapter import NativeSessionAdapter
from thesis_runtime.contracts.messages import decode_transcript
from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations


class ArtifactHarness:
    def __init__(self, content, encoding="utf-8"):
        self.content, self.encoding = content, encoding
        self.reads = []

    def handle(self, request):
        if "id" not in request:
            return None
        result = {}
        if request["method"] == "initialize":
            result = {"protocolVersion": "2024-11-05"}
        elif request["method"] == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "read_artifact",
                        "description": "Read an artifact",
                        "inputSchema": {
                            "type": "object",
                            "required": ["job_id", "name"],
                            "properties": {
                                "job_id": {"type": "string"},
                                "name": {"type": "string"},
                            },
                        },
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
        elif request["method"] == "tools/call":
            arguments = request["params"]["arguments"]
            self.reads.append(arguments)
            result = {
                "isError": False,
                "content": [],
                "structuredContent": {
                    "job_id": arguments["job_id"],
                    "name": arguments["name"],
                    "encoding": self.encoding,
                    "content": self.content,
                },
            }
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}


class ArtifactModel:
    streaming = True

    def __init__(self, arguments):
        self.arguments = list(arguments)
        self.requests = []

    def stream(self, *, messages, role="primary", **kwargs):
        self.requests.append(tuple(messages))
        message = ProviderMessage("assistant", "Done")
        selected = None
        while self.arguments:
            selected = self.arguments[0]
            if selected == "follow":
                previous = next(
                    (
                        json.loads(row.content)
                        for row in reversed(messages)
                        if row.role == "tool"
                    ),
                    None,
                )
                selected = (
                    page(previous["result"])["nextArguments"]
                    if previous is not None
                    else {"max_tokens": 512}
                )
                if selected is None:
                    self.arguments.pop(0)
                    continue
            else:
                self.arguments.pop(0)
            break
        if selected is not None:
            message = ProviderMessage(
                "assistant",
                "",
                tool_calls=(
                    ToolCall(
                        f"read-{len(self.requests)}",
                        "read_artifact",
                        json.dumps(
                            {
                                "job_id": "job-metrics",
                                "name": "metrics.json",
                                **selected,
                            }
                        ),
                    ),
                ),
            )
        yield ModelCompletion(
            message=message,
            model="test",
            provider="test",
            role=role,
            prompt_tokens=100,
            completion_tokens=20,
        )


def run_reads(harness, arguments):
    tools = open_harness_tool_provider(in_process_handler=harness)
    model = ArtifactModel(arguments)
    chat = ChatService()
    conversations = Conversations(
        chat=chat,
        provider=NativeSessionAdapter(model),
        registry_factory=lambda context: build_session_registry(
            context=context, harness_tools=tools
        ),
        spawn_thread=lambda run: None,
    )
    client = TestClient(create_app(bearer_token="token", conversations=conversations))
    thread = chat.create_chat(
        "workspace", title="Artifact pages", owner_actor_id="operator"
    )
    response = client.post(
        f"/v1/workspaces/workspace/chats/{thread.chat_id}/messages",
        headers={"Authorization": "Bearer token"},
        json={"actor_id": "operator", "body": "Inspect the artifact"},
    )
    assert response.status_code == 200, response.text
    assert (
        conversations.run_node_turn("workspace", thread.chat_id, "").status
        == "completed"
    )
    messages = decode_transcript(
        chat.list_conversation_messages("workspace", thread.chat_id, thread.chat_id)
    )
    results = [
        message.content.as_dict()["result"]
        for message in messages
        if message.content.correlation
        and message.content.correlation.tool_name == "read_artifact"
    ]
    return results, model, tools


def page(result):
    return result["datums"][0]["structured"]


def test_large_artifact_is_paged_before_delivery_and_full_reader_is_preserved():
    content = json.dumps(
        {
            "capital": {"initial": "1000000", "final": "1000200"},
            "trades": {
                "rows": [
                    {"row": index, "detail": "trade " * 15} for index in range(100)
                ]
            },
        }
    )
    harness = ArtifactHarness(content)
    results, model, tools = run_reads(
        harness,
        ["follow", {"json_pointer": "/capital"}],
    )
    pages = [page(result) for result in results]
    assert "".join(item["content"] for item in pages[:-1]) == content
    assert pages[0]["truncated"] and pages[0]["nextTokenOffset"] > 0
    assert all(item["tokenCount"] <= 16384 for item in pages)
    assert json.loads(pages[-1]["content"]) == {
        "initial": "1000000",
        "final": "1000200",
    }
    assert pages[-1]["complete"] and pages[-1]["nextArguments"] is None
    assert (
        tools.call_tool(
            "read_artifact", {"job_id": "job-metrics", "name": "metrics.json"}
        ).structured["content"]
        == content
    )
    assert all(set(read) == {"job_id", "name"} for read in harness.reads)


@pytest.mark.parametrize(
    "encoding,content", [("utf-8", 'αβ🙂"\\\n終' * 4), ("hex", "00ff12ab" * 4)]
)
def test_pages_reassemble_exact_content_without_splitting_unicode(encoding, content):
    results, _, _ = run_reads(ArtifactHarness(content * 100, encoding), ["follow"])
    content *= 100
    assert "".join(page(result)["content"] for result in results) == content
    assert page(results[-1])["nextTokenOffset"] is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"max_tokens": 65_537},
        {"token_offset": -1},
        {"token_offset": 1000},
        {"json_pointer": "/missing"},
        {"json_pointer": "/bad~2"},
    ],
)
def test_invalid_page_requests_are_explicit_failures(arguments):
    results, _, _ = run_reads(ArtifactHarness('{"value":1}'), [arguments])
    assert results[0]["ok"] is False
    assert results[0]["failure"]["kind"] in {"invalid_arguments", "schema_validation"}


def test_small_token_budget_handles_json_expansion_without_losing_content():
    content = "\0" * 40
    results, _, _ = run_reads(ArtifactHarness(content), [{"max_tokens": 32}, "follow"])
    assert "".join(page(result)["content"] for result in results) == content
    assert all(page(result)["tokenCount"] <= 32 for result in results)
