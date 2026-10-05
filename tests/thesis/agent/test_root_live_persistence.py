"""Root conversation integration with the live session reply writer."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pytest

from thesis.agent.messages import decode_transcript, from_record
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.session import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis.chat import ChatService, replace_record
from thesis.chat.contracts import ChatMessage, MessageRole
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import (
    ModelCompletion,
    ProviderUnreachableError,
    Role,
    ToolCall,
)
from thesis.providers.session_adapter import (
    NativeSessionAdapter,
    project_native_messages,
)

_WORKSPACE = "live-workspace"
_CALL = ToolCall("root-call", "check", '{"value":42}')


def completion(text: str, *calls: ToolCall) -> ModelCompletion:
    return ModelCompletion(text, "model", "provider", "primary", 0, 0, tool_calls=calls)


class Provider:
    def __init__(self, *responses: ModelCompletion | Exception) -> None:
        self.responses = iter(responses)
        self.requests: list[tuple[ProviderChatMessage, ...]] = []

    def complete(
        self,
        *,
        messages: Sequence[ProviderChatMessage],
        role: Role = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ModelCompletion:
        self.requests.append(tuple(messages))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize("interrupted", [False, True])
def test_root_persists_envelope_and_results_before_next_dispatch(
    interrupted: bool,
) -> None:
    chat = ChatService()
    thread = chat.create_chat(_WORKSPACE, title="Live", owner_actor_id="operator")
    chat.send_message(_WORKSPACE, thread.chat_id, actor_id="operator", body="Check")
    dispatched: list[Mapping[str, object]] = []

    def handler(arguments: Mapping[str, object]) -> ToolExecution:
        envelope = chat.list_messages(_WORKSPACE, thread.chat_id)[-1]
        assert envelope.role is MessageRole.ASSISTANT
        assert envelope.body == "Checking now"
        assert envelope.data is not None
        assert envelope.data["toolCalls"] == [_CALL.as_wire()]
        canonical = from_record(envelope)
        assert canonical.direction == "outgoing"
        assert canonical.message_id == envelope.message_id
        assert envelope.data["sessionMessage"] == canonical.as_dict()
        dispatched.append(arguments)
        return ToolExecution(ok=True, text="Checked", structured={"value": 42})

    class LiveProvider(Provider):
        def complete(
            self,
            *,
            messages: Sequence[ProviderChatMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            if self.requests:
                tool = chat.list_messages(_WORKSPACE, thread.chat_id)[-1]
                assert tool.role is MessageRole.TOOL
                assert tool.data is not None
                assert tool.data["toolCallId"] == _CALL.id
                assert tool.data["arguments"] == {"value": 42}
                assert isinstance(tool.data["durationMs"], int)
                assert tool.data["durationMs"] >= 0
                assert tool.data["invocationId"]
                assert tool.tool_result is not None
                assert tool.tool_result["ok"] is True
                assert messages[-1].tool_call_id == _CALL.id
                canonical = from_record(tool)
                payload = json.loads(messages[-1].content)
                assert payload["text"] == "Checked"
                assert payload["source"] == canonical.content.source == "tool"
                assert "structured" not in payload["result"]
                datums = payload["result"]["datums"]
                assert len(datums) == 1
                assert datums[0]["datumType"] == "check"
                assert datums[0]["ok"] is True
                assert datums[0]["structured"] == {"value": 42}
                assert datums[0]["text"] == "Checked"
                assert canonical.content.as_dict()["result"] == payload["result"]
                assert canonical.content.correlation is not None
                assert payload["correlation"] == canonical.content.correlation.as_dict()
            return super().complete(
                messages=messages,
                role=role,
                tools=tools,
                response_format=response_format,
            )

    provider = LiveProvider(
        completion("Checking now", _CALL),
        ProviderUnreachableError("root model TLS failed")
        if interrupted
        else completion("Finished"),
    )
    registry = SessionToolRegistry(
        [
            RegisteredTool(
                {
                    "type": "function",
                    "function": {"name": "check", "parameters": {"type": "object"}},
                },
                handler,
            )
        ]
    )
    service = Conversations(
        chat=chat,
        provider=provider,
        registry_factory=lambda context: registry,
    )
    root = service.root(_WORKSPACE, thread.chat_id)
    result = root.run_turn()

    assert dispatched == [{"value": 42}]
    durable = chat.list_messages(_WORKSPACE, thread.chat_id)
    assert [message.role for message in durable] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.SYSTEM if interrupted else MessageRole.ASSISTANT,
    ]
    if interrupted:
        assert result is None
        notice = durable[-1]
        assert notice.data is not None
        assert notice.data["status"] == "interrupted"
        assert isinstance(notice.data["failure"], Mapping)
        assert notice.data["failure"]["kind"] == "provider_unreachable"
        assert "root model TLS failed" in notice.body
        assert chat.get_chat(_WORKSPACE, thread.chat_id).state.value == "active"
        # Retry uses complete call/result history without repeating the mutation.
        provider = Provider(completion("Retried without repeating the check"))
        service._provider = NativeSessionAdapter(provider)
        chat.send_message(
            _WORKSPACE, thread.chat_id, actor_id="operator", body="Retry the response"
        )
        assert root.run_turn() is not None
        replay = provider.requests[0]
        index = next(i for i, message in enumerate(replay) if message.tool_calls)
        assert replay[index].tool_calls == (_CALL,)
        assert replay[index + 1].tool_call_id == _CALL.id
        assert replay[index + 1].role == "tool"
        assert dispatched == [{"value": 42}]
        assert replay[1:] == project_native_messages(
            decode_transcript(root.transcript()[:-1])
        )
    else:
        assert isinstance(result, ChatMessage)
        assert result.body == "Finished"
        assert durable[-1].body == "Finished"


def test_root_does_not_dispatch_when_envelope_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = ChatService()
    thread = chat.create_chat(_WORKSPACE, title="Failure", owner_actor_id="operator")
    chat.send_message(_WORKSPACE, thread.chat_id, actor_id="operator", body="Check")
    dispatched: list[str] = []
    registry = SessionToolRegistry(
        [
            RegisteredTool(
                {"type": "function", "function": {"name": "check"}},
                lambda arguments: dispatched.append("check") or ToolExecution(ok=True),
            )
        ]
    )

    def fail_write(*args: object, **kwargs: object) -> ChatMessage:
        raise OSError("durable writer unavailable")

    monkeypatch.setattr(chat, "create_message", fail_write)
    root = Conversations(
        chat=chat,
        provider=Provider(completion("Checking", _CALL)),
        registry_factory=lambda context: registry,
    ).root(_WORKSPACE, thread.chat_id)

    assert root.run_turn() is None
    assert dispatched == []
    assert len(chat.list_messages(_WORKSPACE, thread.chat_id)) == 1


def test_provider_reasoning_is_normalized_before_durable_persistence() -> None:
    chat = ChatService()
    thread = chat.create_chat(_WORKSPACE, title="Reasoning", owner_actor_id="operator")
    chat.send_message(_WORKSPACE, thread.chat_id, actor_id="operator", body="Check")
    provider = Provider(
        ModelCompletion(
            "Finished",
            "model",
            "provider",
            "primary",
            0,
            0,
            reasoning="step\x00 one\r\nstep\x07 two\tend",
        )
    )
    result = (
        Conversations(chat=chat, provider=provider)
        .root(_WORKSPACE, thread.chat_id)
        .run_turn()
    )

    assert isinstance(result, ChatMessage)
    assert result.reasoning == "step one\nstep two\tend"
    assert from_record(result).content.result == {
        "reasoning": "step one\nstep two\tend"
    }


def test_model_only_root_uses_durable_session() -> None:
    chat = ChatService()
    thread = chat.create_chat(_WORKSPACE, title="Fault", owner_actor_id="operator")
    chat.send_message(_WORKSPACE, thread.chat_id, actor_id="operator", body="Check")

    provider = Provider(completion("Finished"))
    root = Conversations(chat=chat, provider=provider).root(_WORKSPACE, thread.chat_id)

    result = root.run_turn()
    assert isinstance(result, ChatMessage)
    assert result.body == "Finished"
    durable = chat.list_messages(_WORKSPACE, thread.chat_id)
    assert len(durable) == 2
    assert durable[-1].role is MessageRole.ASSISTANT
    assert durable[-1].data is not None
    assert durable[-1].data["invocationId"]
    canonical = from_record(durable[-1])
    assert canonical.message_id == result.message_id
    assert canonical.direction == "outgoing"
    assert canonical.content.text == "Finished"
    assert durable[-1].data["sessionMessage"] == canonical.as_dict()

    corrupted_presentation = replace_record(
        durable[-1],
        role=MessageRole.TOOL,
        body="Not the canonical answer",
        tool_result={"ok": False},
        data={
            **durable[-1].data,
            "toolCalls": [{"invalid": "retired native metadata"}],
            "toolCallId": "unrelated",
        },
    )
    assert decode_transcript([corrupted_presentation]) == (canonical,)
    assert project_native_messages([canonical]) == project_native_messages(
        decode_transcript([corrupted_presentation])
    )


def test_root_accepts_tool_result_without_optional_metadata() -> None:
    chat = ChatService()
    thread = chat.create_chat(_WORKSPACE, title="Metadata", owner_actor_id="operator")
    chat.send_message(_WORKSPACE, thread.chat_id, actor_id="operator", body="Check")
    root = Conversations(
        chat=chat,
        provider=Provider(completion("Checking", _CALL), completion("Finished")),
        registry_factory=lambda context: SessionToolRegistry(
            [
                RegisteredTool(
                    {"type": "function", "function": {"name": "check"}},
                    lambda arguments: ToolExecution(ok=True, text="Checked"),
                )
            ]
        ),
    ).root(_WORKSPACE, thread.chat_id)

    result = root.run_turn()

    assert isinstance(result, ChatMessage)
    assert result.body == "Finished"
    tool = chat.list_messages(_WORKSPACE, thread.chat_id)[-2]
    assert tool.role is MessageRole.TOOL
    assert tool.data is not None and tool.data["toolCallId"] == _CALL.id
