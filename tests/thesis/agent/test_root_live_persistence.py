"""Root conversation integration with the live session reply writer."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pytest

from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    ProviderUnreachableError,
    Role,
    ToolCall,
)
from thesis_runtime.adapters.providers.session_adapter import (
    NativeSessionAdapter,
    project_native_messages,
)
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.messages import decode_transcript, from_record
from thesis_runtime.contracts.records import ChatMessage, MessageRole
from thesis_runtime.conversations import ChatService, replace_record
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.tools.registry import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)

_WORKSPACE = "live-workspace"
_CALL = ToolCall("root-call", "check", '{"value":42}')


def completion(text: str, *calls: ToolCall) -> ModelCompletion:
    return ModelCompletion(
        message=ProviderMessage("assistant", text, tool_calls=calls),
        model="model",
        provider="provider",
        role="primary",
        prompt_tokens=0,
        completion_tokens=0,
    )


class Provider:
    def __init__(self, *responses: ModelCompletion | Exception) -> None:
        self.responses = iter(responses)
        self.requests: list[tuple[ProviderMessage, ...]] = []

    def stream(self, *, message_id=None, **kwargs):
        from tests.thesis_runtime.provider_fakes import completion_frames

        yield from completion_frames(self.complete(**kwargs), message_id)

    streaming = True

    def complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("thesis_runtime.sessions.recovery.sleep", lambda _: None)
    chat = ChatService()
    thread = chat.create_chat(_WORKSPACE, title="Live", owner_actor_id="operator")
    chat.send_message(_WORKSPACE, thread.chat_id, actor_id="operator", body="Check")
    dispatched: list[Mapping[str, object]] = []

    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        envelope = chat.list_messages(_WORKSPACE, thread.chat_id)[-1]
        assert envelope.role is MessageRole.ASSISTANT
        assert envelope.body == "Checking now"
        assert envelope.data is not None
        canonical = from_record(envelope)
        assert canonical.direction == "outgoing"
        assert canonical.message_id == envelope.message_id
        assert [request.id for request in canonical.content.requests] == [_CALL.id]
        assert [request.name for request in canonical.content.requests] == [_CALL.name]
        assert envelope.data["sessionMessage"] == canonical.as_dict()
        dispatched.append(arguments)
        return ToolExecution(ok=True, text="Checked", structured={"value": 42})

    class LiveProvider(Provider):
        def stream(self, *, message_id=None, **kwargs):
            from tests.thesis_runtime.provider_fakes import completion_frames

            yield from completion_frames(self.complete(**kwargs), message_id)

        streaming = True

        def complete(
            self,
            *,
            messages: Sequence[ProviderMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            if self.requests:
                tool = next(
                    row
                    for row in chat.list_messages(_WORKSPACE, thread.chat_id)
                    if from_record(row).content.correlation is not None
                )
                assert tool.role is MessageRole.TOOL
                assert tool.data is not None
                canonical = from_record(tool)
                assert canonical.content.correlation is not None
                assert canonical.content.correlation.request_id == _CALL.id
                result_payload = canonical.content.result
                assert result_payload is not None
                assert result_payload["arguments"] == {"value": 42}
                assert isinstance(result_payload["durationMs"], int)
                assert result_payload["durationMs"] >= 0
                assert result_payload["invocationId"]
                assert tool.tool_result is not None
                assert tool.tool_result["ok"] is True
                tool_message = next(
                    message for message in messages if message.tool_call_id == _CALL.id
                )
                payload = json.loads(tool_message.content)
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
        *(
            [ProviderUnreachableError("root model TLS failed")] * 4
            if interrupted
            else [completion("Finished")]
        ),
    )
    registry = SessionToolRegistry(
        [
            RegisteredTool(
                {"name": "check", "parameters": {"type": "object"}},
                handler,
            )
        ]
    )
    service = Conversations(
        chat=chat,
        spawn_thread=lambda _: None,
        provider=NativeSessionAdapter(provider),
        registry_factory=lambda context: registry,
    )
    root = service.root(_WORKSPACE, thread.chat_id)
    result = root.run_turn()

    assert dispatched == [{"value": 42}]
    durable = chat.list_messages(_WORKSPACE, thread.chat_id)
    assert [
        message.role
        for message in durable
        if not (from_record(message).content.result or {}).get("providerRecovery")
    ] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.SYSTEM if interrupted else MessageRole.ASSISTANT,
    ]
    if interrupted:
        assert result is None
        notice = durable[-1]
        assert notice.data is not None
        assert notice.data["status"] == "continuing"
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
                {"name": "check"},
                lambda arguments, context: (
                    dispatched.append("check") or ToolExecution(ok=True)
                ),
            )
        ]
    )

    def fail_write(*args: object, **kwargs: object) -> ChatMessage:
        raise OSError("durable writer unavailable")

    monkeypatch.setattr(chat, "create_message", fail_write)
    root = Conversations(
        chat=chat,
        spawn_thread=lambda _: None,
        provider=NativeSessionAdapter(Provider(completion("Checking", _CALL))),
        registry_factory=lambda context: registry,
    ).root(_WORKSPACE, thread.chat_id)

    with pytest.raises(OSError, match="durable writer unavailable"):
        root.run_turn()
    assert dispatched == []
    durable = chat.list_messages(_WORKSPACE, thread.chat_id)
    assert len(durable) == 1


@pytest.mark.parametrize("steer", [False, True])
def test_repeated_schema_failures_interrupt_and_new_operator_input_resets_budget(
    steer: bool,
) -> None:
    chat = ChatService()
    thread = chat.create_chat(
        _WORKSPACE, title="Schema repair", owner_actor_id="operator"
    )
    chat.send_message(_WORKSPACE, thread.chat_id, actor_id="operator", body="Start")
    dispatched: list[Mapping[str, object]] = []
    schema = {
        "oneOf": [
            {
                "type": "object",
                "required": ["input"],
                "properties": {"input": {"type": "object"}},
            }
        ]
    }

    class RepeatingProvider(Provider):
        def complete(
            self, *, messages, role="primary", tools=None, response_format=None
        ):
            index = len(self.requests)
            if steer and index == 1:
                chat.send_message(
                    _WORKSPACE,
                    thread.chat_id,
                    actor_id="operator",
                    body="New instruction: repair the input type",
                )
            if index < (4 if steer else 3):
                args = {"input": json.dumps({"idea": f"changed prose {index}"})}
                self.responses = iter(
                    [
                        completion(
                            "", ToolCall(f"call-{index}", "launch", json.dumps(args))
                        )
                    ]
                )
            elif steer and index == 4:
                self.responses = iter(
                    [
                        completion(
                            "",
                            ToolCall(
                                "valid-call", "launch", '{"input":{"idea":"Momentum"}}'
                            ),
                        )
                    ]
                )
            else:
                self.responses = iter([completion("Finished")])
            return super().complete(
                messages=messages,
                role=role,
                tools=tools,
                response_format=response_format,
            )

    provider = RepeatingProvider()
    registry = SessionToolRegistry(
        [
            RegisteredTool(
                {"name": "launch", "parameters": schema},
                lambda args, context: (dispatched.append(args), ToolExecution(ok=True))[
                    1
                ],
            )
        ]
    )
    service = Conversations(
        chat=chat,
        spawn_thread=lambda _: None,
        provider=NativeSessionAdapter(provider),
        registry_factory=lambda context: registry,
    )
    result = service.run_node_turn(_WORKSPACE, thread.chat_id, "")
    durable = chat.list_messages(_WORKSPACE, thread.chat_id)
    failures = [
        json.loads(from_record(row).content.as_content())["result"]
        for row in durable
        if row.role is MessageRole.TOOL
    ]
    assert failures[0]["failure"]["violations"] == [
        {"path": "input", "message": "expected object; received string"}
    ]
    assert len(failures[0]["failure"]["message"]) < 150
    if steer:
        assert result.status == "completed"
        assert len(provider.requests) == 6
        assert dispatched == [{"input": {"idea": "Momentum"}}]
        assert any(
            "New instruction" in message.content for message in provider.requests[2]
        )
    else:
        assert result.status == "interrupted"
        assert len(provider.requests) == 3
        assert dispatched == []
        assert (
            json.loads(from_record(durable[-1]).content.as_content())["result"][
                "failure"
            ]["kind"]
            == "tool_schema_loop"
        )
    assert chat.get_chat(_WORKSPACE, thread.chat_id).state.value == "active"


def test_provider_reasoning_is_normalized_before_durable_persistence() -> None:
    chat = ChatService()
    thread = chat.create_chat(_WORKSPACE, title="Reasoning", owner_actor_id="operator")
    chat.send_message(_WORKSPACE, thread.chat_id, actor_id="operator", body="Check")
    provider = Provider(
        ModelCompletion(
            message=ProviderMessage(
                "assistant",
                "Finished",
                continuation={"reasoning": "step\x00 one\r\nstep\x07 two\tend"},
            ),
            model="model",
            provider="provider",
            role="primary",
            prompt_tokens=0,
            completion_tokens=0,
        )
    )
    result = (
        Conversations(chat=chat, provider=NativeSessionAdapter(provider))
        .root(_WORKSPACE, thread.chat_id)
        .run_turn()
    )

    assert isinstance(result, ChatMessage)
    assert result.reasoning == "step one\nstep two\tend"
    canonical = from_record(result)
    assert canonical.content.result is not None
    assert canonical.content.result["reasoning"] == "step one\nstep two\tend"
    continuation = canonical.content.result["provider_continuation"]
    assert isinstance(continuation, Mapping)
    assert continuation["fields"] == {"reasoning": "step\x00 one\r\nstep\x07 two\tend"}


def test_model_only_root_uses_durable_session() -> None:
    chat = ChatService()
    thread = chat.create_chat(_WORKSPACE, title="Fault", owner_actor_id="operator")
    chat.send_message(_WORKSPACE, thread.chat_id, actor_id="operator", body="Check")

    provider = Provider(completion("Finished"))
    root = Conversations(chat=chat, provider=NativeSessionAdapter(provider)).root(
        _WORKSPACE, thread.chat_id
    )

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
        spawn_thread=lambda _: None,
        provider=NativeSessionAdapter(
            Provider(completion("Checking", _CALL), completion("Finished"))
        ),
        registry_factory=lambda context: SessionToolRegistry(
            [
                RegisteredTool(
                    {"name": "check"},
                    lambda arguments, context: ToolExecution(ok=True, text="Checked"),
                )
            ]
        ),
    ).root(_WORKSPACE, thread.chat_id)

    result = root.run_turn()

    assert isinstance(result, ChatMessage)
    assert result.body == "Finished"
    tool = chat.list_messages(_WORKSPACE, thread.chat_id)[-2]
    assert tool.role is MessageRole.TOOL
    canonical = from_record(tool)
    assert canonical.content.correlation is not None
    assert canonical.content.correlation.request_id == _CALL.id
