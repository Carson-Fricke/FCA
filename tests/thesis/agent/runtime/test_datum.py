"""Typed datum contract: multi-datum results, typing, and projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

import pytest

from thesis.agent.runtime.datum import (
    Datum,
    DatumType,
    datum_from_result,
    normalize_datums,
)
from thesis.agent.runtime.session import (
    AgentSession,
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis.agent.session_reply import SessionReplyProvider
from thesis.chat.responder import ToolResultChunk
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ModelCompletion, ToolCall

_WORKSPACE = "ws-test"
_CHAT = "chat-1"
_ACTOR = "operator-1"


def test_datum_requires_a_type_and_freezes_payloads() -> None:
    with pytest.raises(ValueError):
        Datum(datum_type="  ")

    datum = Datum(
        datum_type=DatumType.WORKFLOW_EXECUTION.value,
        structured={"executionId": "exec-1"},
    )

    assert datum.as_dict()["datumType"] == "workflow_execution"
    assert isinstance(datum.structured, MappingProxyType)


def test_normalize_datums_falls_back_to_one_generic_datum() -> None:
    datums = normalize_datums(
        (),
        fallback_type="run_backtest",
        ok=True,
        structured={"sharpe": 1.0},
    )

    assert len(datums) == 1
    assert datums[0].datum_type == "run_backtest"
    assert datums[0].ok is True


def test_normalize_datums_keeps_declared_datums() -> None:
    declared = (
        datum_from_result(DatumType.SUB_AGENT_HANDLE.value, ok=True),
        datum_from_result(DatumType.OPERATOR_QUESTION.value, ok=True),
    )

    assert (
        normalize_datums(declared, fallback_type="spawn_sub_agent", ok=True) == declared
    )


class _FakeProvider:
    def __init__(self, completions: Sequence[ModelCompletion]) -> None:
        self._completions = list(completions)

    def complete(
        self,
        *,
        messages: Sequence[ProviderChatMessage],
        role: str = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ModelCompletion:
        del messages, role, tools, response_format
        return self._completions.pop(0)


def _completion(
    *, content: str = "", tool_calls: tuple[ToolCall, ...] = ()
) -> ModelCompletion:
    return ModelCompletion(
        content=content,
        model="test-model",
        provider="test",
        role="primary",
        prompt_tokens=1,
        completion_tokens=1,
        tool_calls=tool_calls,
    )


def test_one_tool_call_may_project_several_typed_datums() -> None:
    def handler(arguments: Mapping[str, object]) -> ToolExecution:
        del arguments
        return ToolExecution(
            ok=True,
            structured={"summary": "two datums"},
            datums=(
                datum_from_result(
                    DatumType.WORKFLOW_EXECUTION.value,
                    ok=True,
                    structured={"executionId": "exec-1"},
                ),
                datum_from_result(
                    DatumType.SUB_AGENT_HANDLE.value,
                    ok=True,
                    structured={"sub_agent_id": "sa-1"},
                ),
            ),
        )

    registry = SessionToolRegistry(
        [
            RegisteredTool(
                spec={
                    "type": "function",
                    "function": {
                        "name": "start_workflow",
                        "description": "start",
                        "parameters": {},
                    },
                },
                handler=handler,
            )
        ]
    )
    provider = _FakeProvider(
        [
            _completion(
                tool_calls=(ToolCall(id="c1", name="start_workflow", arguments="{}"),)
            ),
            _completion(content="done"),
        ]
    )
    reply = SessionReplyProvider(
        workspace_id=_WORKSPACE,
        chat_id=_CHAT,
        conversation_id=_CHAT,
        provider=provider,
        registry=registry,
        chunk_size=64,
    )

    chunks = list(reply(()))
    tool_chunks = [chunk for chunk in chunks if isinstance(chunk, ToolResultChunk)]

    assert [chunk.datum_type for chunk in tool_chunks] == [
        DatumType.WORKFLOW_EXECUTION.value,
        DatumType.SUB_AGENT_HANDLE.value,
    ]
    assert all(chunk.tool == "start_workflow" for chunk in tool_chunks)
    assert tool_chunks[0].structured == {"executionId": "exec-1"}


def test_undeclared_datums_fall_back_to_the_tool_name() -> None:
    registry = SessionToolRegistry(
        [
            RegisteredTool(
                spec={
                    "type": "function",
                    "function": {
                        "name": "run_backtest",
                        "description": "run",
                        "parameters": {},
                    },
                },
                handler=lambda arguments: ToolExecution(
                    ok=True, structured={"sharpe": 2.0}
                ),
            )
        ]
    )
    provider = _FakeProvider(
        [
            _completion(
                tool_calls=(ToolCall(id="c9", name="run_backtest", arguments="{}"),)
            ),
            _completion(content="done"),
        ]
    )
    session = AgentSession(
        workspace_id=_WORKSPACE,
        chat_id=_CHAT,
        conversation_id=_CHAT,
        provider=provider,
        registry=registry,
    )

    result = session.run([ProviderChatMessage(role="user", content="go")])
    tool_turn = next(turn for turn in result.turns if turn.role == "tool")

    assert [datum.datum_type for datum in tool_turn.datums] == ["run_backtest"]
