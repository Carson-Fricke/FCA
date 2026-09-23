"""Unified agent session tool-loop behavior."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from thesis.agent.runtime.session import (
    AgentSession,
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat.contracts import AgentInvocation, AgentInvocationKind
from thesis.chat.service import ChatService, InMemoryChatPersistence
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ModelCompletion, ToolCall

_WORKSPACE = "ws-test"
_ACTOR = "operator-1"


def _completion(
    *,
    content: str = "",
    tool_calls: tuple[ToolCall, ...] = (),
    reasoning: str = "",
) -> ModelCompletion:
    return ModelCompletion(
        content=content,
        model="test-model",
        provider="test",
        role="primary",
        prompt_tokens=1,
        completion_tokens=1,
        reasoning=reasoning,
        tool_calls=tool_calls,
    )


class _FakeProvider:
    def __init__(self, completions: Sequence[ModelCompletion]) -> None:
        self._completions = list(completions)
        self.calls: list[Sequence[ProviderChatMessage]] = []

    def complete(
        self,
        *,
        messages: Sequence[ProviderChatMessage],
        role: str = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ModelCompletion:
        del role, tools, response_format
        self.calls.append(tuple(messages))
        return self._completions.pop(0)


def _function_spec(name: str) -> Mapping[str, object]:
    return {
        "type": "function",
        "function": {"name": name, "description": name, "parameters": {}},
    }


def _session(provider: _FakeProvider, tools: Sequence[RegisteredTool] = ()):
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    store = PersistenceSubAgentStore(chat.persistence)
    registry = SessionToolRegistry(tools)
    session = AgentSession(
        workspace_id=_WORKSPACE,
        chat_id="chat-1",
        conversation_id="subagent-1",
        provider=provider,
        registry=registry,
        store=store,
        sub_agent_id="subagent-1",
    )
    return session, store


def test_session_dispatches_tool_then_returns_text() -> None:
    captured: list[Mapping[str, object]] = []

    def handler(arguments: Mapping[str, object]) -> ToolExecution:
        captured.append(dict(arguments))
        return ToolExecution(ok=True, structured={"ran": True}, text="done")

    provider = _FakeProvider(
        [
            _completion(
                tool_calls=(ToolCall(id="call-1", name="probe", arguments="{}"),),
            ),
            _completion(content="final answer"),
        ]
    )
    session, _ = _session(provider, [RegisteredTool(_function_spec("probe"), handler)])
    result = session.run([ProviderChatMessage(role="user", content="go")])

    assert result.status == "completed"
    assert result.content == "final answer"
    assert captured == [{}]
    roles = [turn.role for turn in result.turns]
    assert roles == ["assistant", "tool", "assistant"]


def test_session_persists_model_and_tool_invocations() -> None:
    provider = _FakeProvider(
        [
            _completion(
                tool_calls=(ToolCall(id="call-1", name="probe", arguments="{}"),),
            ),
            _completion(content="final"),
        ]
    )
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    store = PersistenceSubAgentStore(chat.persistence)
    registry = SessionToolRegistry(
        [
            RegisteredTool(
                _function_spec("probe"),
                lambda arguments: ToolExecution(ok=True),
            )
        ]
    )
    session = AgentSession(
        workspace_id=_WORKSPACE,
        chat_id="chat-1",
        conversation_id="subagent-1",
        provider=provider,
        registry=registry,
        store=store,
        sub_agent_id="subagent-1",
    )
    session.run([ProviderChatMessage(role="user", content="go")])

    invocations = [
        record for record in chat.persistence.list(AgentInvocation, _WORKSPACE)
    ]
    kinds = [record.kind for record in invocations]
    assert AgentInvocationKind.TOOL in kinds
    assert AgentInvocationKind.MODEL in kinds
    tool_records = [
        record for record in invocations if record.kind is AgentInvocationKind.TOOL
    ]
    assert tool_records[-1].tool_name == "probe"


def test_terminal_tool_stops_the_session_with_a_completion() -> None:
    provider = _FakeProvider(
        [
            _completion(
                tool_calls=(
                    ToolCall(id="call-1", name="complete", arguments='{"ok":true}'),
                ),
            )
        ]
    )
    session, _ = _session(
        provider,
        [
            RegisteredTool(
                _function_spec("complete"),
                lambda arguments: ToolExecution(
                    ok=True, structured=dict(arguments), terminal=True
                ),
            )
        ],
    )
    result = session.run([ProviderChatMessage(role="user", content="go")])
    assert result.stopped_for_completion
    assert result.completion == {"ok": True}


def test_unknown_tool_returns_failure_and_continues() -> None:
    provider = _FakeProvider(
        [
            _completion(
                tool_calls=(ToolCall(id="call-1", name="ghost", arguments="{}"),),
            ),
            _completion(content="recovered"),
        ]
    )
    session, _ = _session(provider)
    result = session.run([ProviderChatMessage(role="user", content="go")])
    assert result.content == "recovered"
    assert result.turns[1].role == "tool"
    assert '"ok":false' in result.turns[1].content


def test_iteration_cap_stops_a_runaway_loop() -> None:
    tool_call = ToolCall(id="call-1", name="probe", arguments="{}")
    provider = _FakeProvider([_completion(tool_calls=(tool_call,))] * 3)
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    registry = SessionToolRegistry(
        [
            RegisteredTool(
                _function_spec("probe"),
                lambda arguments: ToolExecution(ok=True),
            )
        ]
    )
    session = AgentSession(
        workspace_id=_WORKSPACE,
        chat_id="chat-1",
        conversation_id="subagent-1",
        provider=provider,
        registry=registry,
        max_tool_iterations=3,
    )
    result = session.run([ProviderChatMessage(role="user", content="go")])
    assert result.status == "iterations_exhausted"


def _capturing_tool(captured: list[Mapping[str, object]]) -> RegisteredTool:
    def handler(arguments: Mapping[str, object]) -> ToolExecution:
        captured.append(dict(arguments))
        return ToolExecution(ok=True, structured={"ran": True}, text="done")

    return RegisteredTool(_function_spec("probe"), handler)


def test_fenced_tool_arguments_are_recovered_in_turn() -> None:
    captured: list[Mapping[str, object]] = []
    provider = _FakeProvider(
        [
            _completion(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="probe",
                        arguments='```json\n{"payload": "fenced"}\n```',
                    ),
                )
            ),
            _completion(content="recovered"),
        ]
    )
    session, _ = _session(provider, [_capturing_tool(captured)])
    result = session.run([ProviderChatMessage(role="user", content="go")])

    assert captured == [{"payload": "fenced"}]
    assert result.content == "recovered"


def test_prose_wrapped_tool_arguments_are_recovered_in_turn() -> None:
    captured: list[Mapping[str, object]] = []
    provider = _FakeProvider(
        [
            _completion(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="probe",
                        arguments='Here is the call: {"payload": "embedded"} — enjoy',
                    ),
                )
            ),
            _completion(content="recovered"),
        ]
    )
    session, _ = _session(provider, [_capturing_tool(captured)])
    result = session.run([ProviderChatMessage(role="user", content="go")])

    assert captured == [{"payload": "embedded"}]
    assert result.content == "recovered"


def test_unrecoverable_arguments_fail_with_an_actionable_message() -> None:
    provider = _FakeProvider(
        [
            _completion(
                tool_calls=(
                    ToolCall(id="call-1", name="probe", arguments="not json at all"),
                )
            ),
            _completion(content="recovered"),
        ]
    )
    session, _ = _session(provider)
    result = session.run([ProviderChatMessage(role="user", content="go")])

    assert result.content == "recovered"
    assert result.turns[1].role == "tool"
    assert "tolerant recovery" in result.turns[1].content
