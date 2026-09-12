"""Harness tool-calling reply provider coverage for the Thesis agent.

These tests prove the bounded function-calling loop end to end at the provider
seam: the tool catalog is sent with the completion, requested tool calls are
dispatched through the harness MCP provider, structured tool results (and
failures) are threaded back as ``role: tool`` messages, the iteration cap
forces a text answer, and an unusable catalog degrades to a plain reply.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from thesis.agent.harness_tools import (
    HARNESS_TOOL_NAMES,
    InProcessMcpHandler,
    open_harness_tool_provider,
)
from thesis.agent.reply import (
    harness_agent_reply_provider,
    reply_provider_for,
)
from thesis.chat.contracts import ChatMessage, MessageRole
from thesis.integrations.mcp import HarnessMcpTopology
from thesis.providers.openrouter import OpenRouterProfile, OpenRouterProvider
from thesis.providers.transports import (
    DEFAULT_SYSTEM_PROMPT,
    HARNESS_TOOL_SYSTEM_PROMPT,
)

SECRET = "sk-or-secret-must-not-escape"


class _KeySource:
    def key(self) -> str:
        return SECRET


class _QueueTransport:
    """Return queued completion bodies in order and record every request."""

    def __init__(self, bodies: Sequence[str]) -> None:
        self._bodies = list(bodies)
        self.calls: list[tuple[str, Mapping[str, str], dict[str, Any]]] = []

    def post(
        self,
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        self.calls.append((path, dict(headers), dict(payload)))
        if not self._bodies:
            raise AssertionError("provider received more completions than expected")
        return 200, self._bodies.pop(0)


class _HarnessHandler:
    """In-process MCP handler publishing every harness tool."""

    def __init__(self, *, is_error: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self._is_error = is_error

    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        if "id" not in message:
            return None
        method = message.get("method")
        if method == "initialize":
            result: dict[str, object] = {"protocolVersion": "2024-11-05"}
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": f"{name} tool",
                        "inputSchema": {"type": "object"},
                    }
                    for name in HARNESS_TOOL_NAMES
                ]
            }
        elif method == "tools/call":
            params = message.get("params")
            assert isinstance(params, dict)
            self.calls.append(params)
            if self._is_error:
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": "boom"}],
                    "structuredContent": {"state": "failed"},
                }
            else:
                result = {
                    "isError": False,
                    "content": [{"type": "text", "text": "ok"}],
                    "structuredContent": {
                        "jobId": "job-1",
                        "tool": params.get("name"),
                    },
                }
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


class _BrokenHandler:
    """In-process MCP handler whose tool catalog always errors."""

    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {"code": -32603, "message": "catalog exploded"},
        }


def _profile() -> OpenRouterProfile:
    return OpenRouterProfile(profile_id="openrouter-test", model="test/model")


def _provider(transport: _QueueTransport) -> OpenRouterProvider:
    return OpenRouterProvider(_profile(), _KeySource(), transport)


def _tools(handler: InProcessMcpHandler):
    return open_harness_tool_provider(
        HarnessMcpTopology(thesis_location="local", harness_location="local"),
        in_process_handler=handler,
    )


def _text_body(text: str) -> str:
    return json.dumps(
        {
            "model": "test/model",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )


def _tool_call_body(
    name: str, arguments: Mapping[str, object], *, call_id: str = "call-1"
) -> str:
    return _raw_tool_call_body(name, json.dumps(arguments), call_id=call_id)


def _raw_tool_call_body(name: str, arguments: str, *, call_id: str = "call-1") -> str:
    return json.dumps(
        {
            "model": "test/model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )


def _history(*bodies: str) -> tuple[ChatMessage, ...]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return tuple(
        ChatMessage(
            workspace_id="workspace-a",
            created_at=now,
            created_by="operator",
            message_id=f"message-{index}",
            chat_id="chat-1",
            actor_id="operator",
            role=MessageRole.USER,
            body=body,
        )
        for index, body in enumerate(bodies)
    )


def test_tool_loop_dispatches_and_threads_structured_results() -> None:
    handler = _HarnessHandler()
    tools = _tools(handler)
    transport = _QueueTransport(
        [
            _tool_call_body("run_backtest", {"config_path": "cfg.yaml"}),
            _text_body("Backtest launched."),
        ]
    )
    stream = harness_agent_reply_provider(_provider(transport), tools, chunk_size=5)

    reply = "".join(stream(_history("run a backtest")))

    assert reply == "Backtest launched."
    first = transport.calls[0][2]
    assert first["tools"][0]["function"]["name"] == HARNESS_TOOL_NAMES[0]
    assert first["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert handler.calls == [
        {"name": "run_backtest", "arguments": {"config_path": "cfg.yaml"}}
    ]
    second_messages = transport.calls[1][2]["messages"]
    assistant = next(item for item in second_messages if item["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["name"] == "run_backtest"
    tool_message = next(item for item in second_messages if item["role"] == "tool")
    assert tool_message["tool_call_id"] == "call-1"
    result = json.loads(tool_message["content"])
    assert result["ok"] is True
    assert result["structured"]["jobId"] == "job-1"


def test_tool_prompt_forbids_reasoning_and_pins_operator_language() -> None:
    handler = _HarnessHandler()
    transport = _QueueTransport([_text_body("Hola.")])
    stream = harness_agent_reply_provider(_provider(transport), _tools(handler))

    "".join(stream(_history("hola")))

    system = transport.calls[0][2]["messages"][0]["content"]
    assert system == HARNESS_TOOL_SYSTEM_PROMPT
    assert "same language" in system
    assert "reasoning" in system
    assert "chain-of-thought" in system
    assert "harness" in system.lower()


def test_tool_failure_is_threaded_as_tool_message_not_raised() -> None:
    handler = _HarnessHandler(is_error=True)
    transport = _QueueTransport(
        [
            _tool_call_body("get_run", {"run_id": "run-1"}),
            _text_body("The run query failed."),
        ]
    )
    stream = harness_agent_reply_provider(_provider(transport), _tools(handler))

    reply = "".join(stream(_history("get run run-1")))

    assert reply == "The run query failed."
    tool_message = next(
        item for item in transport.calls[1][2]["messages"] if item["role"] == "tool"
    )
    result = json.loads(tool_message["content"])
    assert result["ok"] is False
    assert result["failure"]["kind"] == "tool"


def test_malformed_tool_arguments_become_a_tool_message() -> None:
    handler = _HarnessHandler()
    transport = _QueueTransport(
        [
            _raw_tool_call_body("get_run", "not-json"),
            _text_body("Recovered."),
        ]
    )
    stream = harness_agent_reply_provider(_provider(transport), _tools(handler))

    reply = "".join(stream(_history("go")))

    assert reply == "Recovered."
    tool_message = next(
        item for item in transport.calls[1][2]["messages"] if item["role"] == "tool"
    )
    assert "valid JSON" in json.loads(tool_message["content"])["error"]
    assert handler.calls == []


def test_iteration_cap_forces_final_answer_without_tools() -> None:
    handler = _HarnessHandler()
    bodies = [
        _tool_call_body("get_run", {"run_id": "run-1"}, call_id="call-1"),
        _tool_call_body("get_run", {"run_id": "run-2"}, call_id="call-2"),
        _text_body("Final answer."),
    ]
    transport = _QueueTransport(bodies)
    stream = harness_agent_reply_provider(
        _provider(transport), _tools(handler), max_tool_iterations=2
    )

    reply = "".join(stream(_history("keep querying")))

    assert reply == "Final answer."
    assert len(transport.calls) == 3
    assert "tools" not in transport.calls[2][2]
    assert len(handler.calls) == 2


def test_unusable_catalog_falls_back_to_plain_reply() -> None:
    tools = open_harness_tool_provider(
        HarnessMcpTopology(thesis_location="local", harness_location="local"),
        in_process_handler=_BrokenHandler(),
    )
    transport = _QueueTransport([_text_body("Plain answer.")])
    stream = harness_agent_reply_provider(_provider(transport), tools)

    reply = "".join(stream(_history("hi")))

    assert reply == "Plain answer."
    assert "tools" not in transport.calls[0][2]


def test_reply_provider_for_selects_tool_aware_or_plain_provider() -> None:
    handler = _HarnessHandler()
    tool_transport = _QueueTransport(
        [
            _tool_call_body("get_run", {"run_id": "run-1"}),
            _text_body("Tool answer."),
        ]
    )
    with_tools = reply_provider_for(_provider(tool_transport), _tools(handler))

    assert "".join(with_tools(_history("query"))) == "Tool answer."
    assert "tools" in tool_transport.calls[0][2]

    plain_transport = _QueueTransport([_text_body("Plain answer.")])
    without_tools = reply_provider_for(_provider(plain_transport), None)

    assert "".join(without_tools(_history("hello"))) == "Plain answer."
    assert "tools" not in plain_transport.calls[0][2]
    assert (
        plain_transport.calls[0][2]["messages"][0]["content"] == DEFAULT_SYSTEM_PROMPT
    )


def test_provider_construction_validates_loop_settings() -> None:
    handler = _HarnessHandler()
    tools = _tools(handler)
    transport = _QueueTransport([_text_body("x")])
    provider = _provider(transport)

    with pytest.raises(ValueError, match="chunk_size"):
        harness_agent_reply_provider(provider, tools, chunk_size=0)
    with pytest.raises(ValueError, match="max_tool_iterations"):
        harness_agent_reply_provider(provider, tools, max_tool_iterations=0)


def test_local_composition_selects_the_tool_aware_provider() -> None:
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "local"
        / "thesis_local_api.py"
    )
    source = script.read_text(encoding="utf-8")

    assert "open_harness_tool_provider(" in source
    assert "reply_provider_for(" in source
    assert "openrouter_reply_provider" not in source
