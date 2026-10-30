"""Canonical session serialization, scope decoding, and native projection."""

import hashlib
import json
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import cast

import pytest

from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    ProviderRejectedError,
    Role,
    ToolCall,
    messages_payload,
)
from thesis_runtime.adapters.providers.session_adapter import (
    NativeSessionAdapter,
    project_native_messages,
)
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    ToolRequest,
    decode_transcript,
    from_record,
)
from thesis_runtime.contracts.records import ChatMessage as Record
from thesis_runtime.contracts.records import MessageRole


def envelope(message_id: str = "request", *ids: str) -> SessionMessage:
    return SessionMessage(
        message_id,
        "outgoing",
        MessageContent(
            "Checking",
            "agent",
            requests=tuple(ToolRequest(i, "check", "{}") for i in ids),
        ),
    )


def incoming(
    message_id: str,
    *,
    source: str = "user",
    correlation: ToolCorrelation | None = None,
) -> SessionMessage:
    return SessionMessage(
        message_id,
        "incoming",
        MessageContent(
            message_id,
            source,
            correlation,
            {"ok": False, "failure": {"message": "failed"}} if correlation else None,
        ),
    )


def datum(message_id: str, call_id: str, request: str = "request") -> SessionMessage:
    return incoming(
        message_id,
        source="tool_result",
        correlation=ToolCorrelation(request, call_id, "check"),
    )


def record(
    message_id: str,
    *,
    role: MessageRole = MessageRole.USER,
    data: Mapping[str, object] | None = None,
    result: Mapping[str, object] | None = None,
    chat_id: str = "chat",
) -> Record:
    return Record(
        workspace_id="workspace",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_by="runtime",
        message_id=message_id,
        chat_id=chat_id,
        actor_id="runtime",
        role=role,
        body="Evidence not a call-ID heuristic",
        data=data,
        tool_result=result,
        conversation_id=chat_id,
        root_conversation_id=chat_id,
        parent_conversation_id=None,
    )


def test_exact_neutral_fields_and_round_trip() -> None:
    assert [field.name for field in fields(SessionMessage)] == [
        "message_id",
        "direction",
        "content",
        "position",
    ]
    assert [field.name for field in fields(MessageContent)] == [
        "text",
        "source",
        "correlation",
        "result",
        "requests",
    ]
    request = ToolRequest.from_provider(ToolCall("call", "check", "malformed JSON"))
    assert request.as_dict() == {
        "id": "call",
        "name": "check",
        "arguments": "malformed JSON",
    }
    assert not hasattr(request, "as_wire")

    for message in (envelope("request", "call"), datum("result", "call")):
        assert (
            SessionMessage.from_dict(json.loads(json.dumps(message.as_dict())))
            == message
        )
        assert (
            from_record(
                record(message.message_id, data={"sessionMessage": message.as_dict()})
            )
            == message
        )
    with pytest.raises(ValueError):
        SessionMessage.from_dict({**incoming("x").as_dict(), "role": "system"})
    with pytest.raises(ValueError, match="identity"):
        from_record(record("other", data={"sessionMessage": incoming("x").as_dict()}))


@pytest.mark.parametrize("matched", [False, True])
@pytest.mark.parametrize("same_arguments", [False, True])
def test_projection_omits_only_duplicate_arguments_of_an_exact_complete_call(
    matched, same_arguments
):
    request = envelope("request", "call")
    receipt = SessionMessage(
        "receipt",
        "incoming",
        MessageContent(
            "Result",
            "tool",
            ToolCorrelation("request" if matched else "foreign", "call", "check"),
            {
                "arguments": {} if same_arguments else {"distinct": True},
                "structured": {"evidence": "preserved"},
            },
        ),
    )
    before = receipt.as_dict()
    projected = project_native_messages((request, receipt))
    value = json.loads(projected[-1].content)
    assert ("arguments" in value["result"]) is not (matched and same_arguments)
    assert value["result"]["structured"] == {"evidence": "preserved"}
    assert receipt.as_dict() == before
    replay = tuple(
        SessionMessage.from_dict(item.as_dict()) for item in (request, receipt)
    )
    assert project_native_messages(replay) == projected


def test_content_is_deeply_immutable_and_serialization_detached() -> None:
    mutable = {"items": [{"value": 1}]}
    content = MessageContent("text", "delivery", result=mutable)
    mutable["items"][0]["value"] = 2
    assert json.loads(content.as_content())["result"] == {"items": [{"value": 1}]}
    with pytest.raises(TypeError):
        cast(MutableMapping[str, object], content.result)["new"] = 1
    with pytest.raises(FrozenInstanceError):
        setattr(content, "source", "other")  # noqa: B010 - exercise frozen assignment
    detached = content.as_dict()
    detached["result"] = {}
    assert json.loads(content.as_content())["result"] == {"items": [{"value": 1}]}


@pytest.mark.parametrize(
    "source",
    [
        "user",
        "child",
        "agent_prompt",
        "delivery",
        "runtime",
        "tool_result",
        "system",
        "instruction",
    ],
)
def test_all_incoming_sources_use_one_json_content_without_authority(
    source: str,
) -> None:
    message = SessionMessage(
        "input", "incoming", MessageContent("text", source, result={"ok": False})
    )
    native = project_native_messages([message])
    assert native == (ProviderMessage("user", message.content.as_content()),)
    assert json.loads(native[0].content) == {
        "source": source,
        "text": "text",
        "result": {"ok": False},
        "correlation": None,
    }


def test_directions_not_sources_choose_native_roles() -> None:
    messages = [
        SessionMessage("instruction", "instruction", MessageContent("Rules", "user")),
        SessionMessage("answer", "outgoing", MessageContent("Answer", "runtime")),
    ]
    assert project_native_messages(messages) == (
        ProviderMessage("system", "Rules"),
        ProviderMessage("assistant", "Answer"),
    )


def test_explicit_canonical_instruction_retains_trusted_port_direction() -> None:
    instruction = SessionMessage(
        "configured", "instruction", MessageContent("Rules", "config")
    )
    durable = record("configured", data={"sessionMessage": instruction.as_dict()})
    assert from_record(durable) == instruction
    assert project_native_messages(decode_transcript([durable])) == (
        ProviderMessage("system", "Rules"),
    )


def test_empty_outgoing_text_preserves_evidence_without_invalid_native_content() -> (
    None
):
    message = SessionMessage("empty", "outgoing", MessageContent("", "agent"))
    assert project_native_messages([message]) == (
        ProviderMessage("assistant", message.content.as_content()),
    )


def test_delayed_results_defer_interleaved_inputs_without_mutating_arrivals() -> None:
    messages = (
        envelope("request", "a", "b"),
        incoming("steering"),
        datum("B", "b"),
        incoming("child", source="child"),
        datum("A", "a"),
    )
    snapshot = [message.as_dict() for message in messages]
    native = project_native_messages(messages)
    assert [message.role for message in native] == [
        "assistant",
        "tool",
        "tool",
        "user",
        "user",
    ]
    assert [message.tool_call_id for message in native[1:3]] == ["a", "b"]
    assert json.loads(native[3].content)["text"] == "steering"
    assert json.loads(native[4].content)["source"] == "child"
    assert [message.as_dict() for message in messages] == snapshot
    assert project_native_messages(messages) == native


def test_multiple_datums_aggregate_all_evidence_in_arrival_order() -> None:
    messages = [
        envelope("request", "a"),
        datum("one", "a"),
        incoming("note"),
        datum("two", "a"),
        datum("three", "a"),
    ]
    native = project_native_messages(messages)
    assert [message.role for message in native] == ["assistant", "tool", "user"]
    assert [item["text"] for item in json.loads(native[1].content)["datums"]] == [
        "one",
        "two",
        "three",
    ]
    assert all(
        not item["result"]["ok"] for item in json.loads(native[1].content)["datums"]
    )


def test_partial_group_preserves_inputs_without_fabricated_tool_responses() -> None:
    messages = [envelope("request", "a", "b"), datum("one", "a"), incoming("steering")]
    native = project_native_messages(messages)
    assert [message.role for message in native] == ["user", "user", "user"]
    assert "missing results" in native[0].content
    assert '"id":"b"' in native[0].content
    assert native[1].content == messages[1].content.as_content()
    assert native[2].content == messages[2].content.as_content()


@pytest.mark.parametrize(
    "correlation",
    [
        ToolCorrelation("foreign", "a", "check"),
        ToolCorrelation("request", "other", "check"),
        ToolCorrelation("request", "a", "wrong"),
    ],
)
def test_foreign_correlation_stays_ordinary_incoming_content(
    correlation: ToolCorrelation,
) -> None:
    foreign = incoming("foreign", correlation=correlation)
    native = project_native_messages(
        [envelope("request", "a"), foreign, datum("valid", "a")]
    )
    assert [message.role for message in native] == ["assistant", "tool", "user"]
    assert native[-1].content == foreign.content.as_content()
    assert json.loads(native[1].content)["text"] == "valid"


def test_duplicate_request_ids_across_envelopes_never_accidentally_associate() -> None:
    messages = [
        envelope("first", "a"),
        envelope("second", "a"),
        datum("second-result", "a", "second"),
        datum("first-result", "a", "first"),
    ]
    native = project_native_messages(messages)
    assert all(message.role == "user" for message in native)
    assert "duplicate/reused request IDs" in native[0].content
    assert "duplicate/reused request IDs" in native[1].content
    assert native[2].content == messages[2].content.as_content()
    assert native[3].content == messages[3].content.as_content()


def test_duplicate_ids_within_envelope_degrade_and_duplicate_message_ids_reject() -> (
    None
):
    native = project_native_messages(
        [envelope("request", "a", "a"), datum("result", "a")]
    )
    assert [message.role for message in native] == ["user", "user"]
    with pytest.raises(ValueError, match="unique"):
        project_native_messages([incoming("same"), incoming("same")])


def test_explicit_join_survives_other_outputs_but_excludes_early_arrival() -> None:
    messages = [
        datum("early", "a"),
        envelope("request", "a"),
        SessionMessage("text", "outgoing", MessageContent("Answer", "agent")),
        datum("late", "a"),
    ]
    native = project_native_messages(messages)
    assert [message.role for message in native] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert native[0].content == messages[0].content.as_content()
    assert json.loads(native[2].content)["text"] == "late"


def test_early_result_alone_does_not_complete_a_later_request() -> None:
    messages = [datum("early", "a"), envelope("request", "a")]
    native = project_native_messages(messages)
    assert [message.role for message in native] == ["user", "user"]
    assert native[0].content == messages[0].content.as_content()
    assert "missing results" in native[1].content


def test_replay_is_byte_identical_for_same_scope() -> None:
    messages = [
        envelope("request", "a", "b"),
        incoming("steering"),
        datum("B", "b"),
        datum("A", "a"),
    ]
    replay = [SessionMessage.from_dict(message.as_dict()) for message in messages]
    assert json.dumps(
        messages_payload(project_native_messages(messages))
    ) == json.dumps(messages_payload(project_native_messages(replay)))


def test_canonical_decode_overrides_legacy_metadata_and_round_trip_projects_same() -> (
    None
):
    messages = [envelope("request", "call"), datum("result", "call")]
    records = [
        record(
            message.message_id,
            data={"sessionMessage": message.as_dict(), "toolCalls": "obsolete"},
        )
        for message in messages
    ]
    assert decode_transcript(records) == tuple(messages)
    assert project_native_messages(
        decode_transcript(records)
    ) == project_native_messages(messages)


def test_adapter_forwards_options_and_returns_canonical_outgoing_message() -> None:
    completion = ModelCompletion(
        message=ProviderMessage(
            "assistant", "Answer", tool_calls=(ToolCall("call", "check", "{}"),)
        ),
        model="model",
        provider="provider",
        role="research",
        prompt_tokens=1,
        completion_tokens=2,
    )
    seen: dict[str, object] = {}

    class Provider:
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
            seen.update(
                messages=messages,
                role=role,
                tools=tools,
                response_format=response_format,
            )
            return completion

    messages = [incoming("input")]
    tools = [{"name": "check"}]
    response_format = {"type": "json_object"}
    assert NativeSessionAdapter(Provider(), id_factory=lambda: "output").complete(
        messages=messages,
        role="research",
        tools=tools,
        response_format=response_format,
    ) == SessionMessage(
        "output",
        "outgoing",
        MessageContent(
            "Answer", "agent", requests=(ToolRequest("call", "check", "{}"),)
        ),
    )
    assert seen == {
        "messages": project_native_messages(messages),
        "role": "research",
        "tools": [{"type": "function", "function": tool} for tool in tools],
        "response_format": response_format,
    }


def test_adapter_ids_are_fresh_and_reasoning_stays_separate() -> None:
    class Provider:
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
            return ModelCompletion(
                message=ProviderMessage(
                    "assistant", "Answer", continuation={"reasoning": "Reasoning"}
                ),
                model="model",
                provider="provider",
                role=role,
                prompt_tokens=1,
                completion_tokens=2,
            )

    adapter = NativeSessionAdapter(Provider())
    first = adapter.complete(messages=[incoming("input")])
    second = adapter.complete(messages=[incoming("input"), first])
    assert first.message_id != second.message_id
    assert first.direction == "outgoing"
    assert first.content.text == "Answer"
    assert first.content.source == "agent"
    assert first.content.result is not None
    assert first.content.result["reasoning"] == "Reasoning"
    assert SessionMessage.from_dict(first.as_dict()) == first
    assert project_native_messages([first]) == (
        ProviderMessage("assistant", "Answer", continuation={"reasoning": "Reasoning"}),
    )
    with pytest.raises(ValueError, match="fresh"):
        NativeSessionAdapter(Provider(), id_factory=lambda: "input").complete(
            messages=[incoming("input")]
        )


def test_adapter_normalizes_provider_reasoning_before_projection() -> None:
    class Provider:
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
            return ModelCompletion(
                message=ProviderMessage(
                    "assistant",
                    "Answer",
                    continuation={"reasoning": "step\x00 one\r\nstep\x07 two\tend"},
                ),
                model="model",
                provider="provider",
                role=role,
                prompt_tokens=1,
                completion_tokens=2,
            )

    output = NativeSessionAdapter(Provider()).complete(messages=[incoming("input")])
    assert output.content.result is not None
    assert output.content.result["reasoning"] == "step one\nstep two\tend"
    assert project_native_messages([output])[0].continuation == {
        "reasoning": "step\x00 one\r\nstep\x07 two\tend"
    }


def test_durable_decode_restores_assigned_position_without_mutating_content() -> None:
    message = incoming("input")
    durable = replace(
        record("input", data={"sessionMessage": message.as_dict()}), input_position=17
    )
    decoded = from_record(durable)
    assert decoded == replace(message, position=17)
    assert message.position == 0


def test_decode_omits_started_presentation_but_preserves_canonical_input() -> None:
    started = record(
        "started", data={"activityKind": "runtime_tool", "toolActivity": "started"}
    )
    finished_message = SessionMessage(
        "finished", "incoming", MessageContent("Finished", "runtime")
    )
    finished = record(
        "finished",
        data={
            "sessionMessage": finished_message.as_dict(),
            "activityKind": "runtime_tool",
            "toolActivity": "finished",
        },
    )
    canonical = incoming("canonical", source="runtime")
    assert decode_transcript(
        [
            started,
            finished,
            record(
                "canonical",
                data={"sessionMessage": canonical.as_dict(), "toolActivity": "started"},
            ),
        ]
    ) == (finished_message, canonical)


@pytest.mark.parametrize("text", ["", " \t\r\n"])
def test_tool_only_completion_maps_raw_arguments_without_native_fields(
    text: str,
) -> None:
    class Provider:
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
            return ModelCompletion(
                message=ProviderMessage(
                    "assistant",
                    text,
                    tool_calls=(ToolCall("call", "check", "malformed JSON"),),
                ),
                model="model",
                provider="provider",
                role=role,
                prompt_tokens=1,
                completion_tokens=2,
            )

    output = NativeSessionAdapter(Provider(), id_factory=lambda: "output").complete(
        messages=[incoming("input")]
    )
    assert output == SessionMessage(
        "output",
        "outgoing",
        MessageContent(
            text, "agent", requests=(ToolRequest("call", "check", "malformed JSON"),)
        ),
    )
    assert output.content.result is None


@pytest.mark.parametrize("text", ["", " ", "\t\r\n"])
@pytest.mark.parametrize("reasoning", ["", "Reasoning without an answer"])
def test_blank_native_reply_is_rejected_with_reserved_output_identity(
    text: str, reasoning: str
) -> None:
    calls: list[str] = []

    class Provider:
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
            calls.append("provider")
            return ModelCompletion(
                message=ProviderMessage(
                    "assistant", text, continuation={"reasoning": reasoning}
                ),
                model="model",
                provider="provider",
                role=role,
                prompt_tokens=1,
                completion_tokens=2,
            )

    def new_id() -> str:
        calls.append("id")
        return "output"

    messages = [incoming("input")]
    canonical = [message.as_dict() for message in messages]
    with pytest.raises(
        ProviderRejectedError, match="no answer/tool requests"
    ) as raised:
        NativeSessionAdapter(Provider(), id_factory=new_id).complete(messages=messages)
    assert raised.value.status is None
    assert calls == ["id", "provider"]
    assert [message.as_dict() for message in messages] == canonical


def test_uncorrelated_prose_never_becomes_a_native_result() -> None:
    message = SessionMessage(
        "prose",
        "incoming",
        MessageContent(
            "Result for request call a from check",
            "tool_result",
            result={"callId": "a"},
        ),
    )
    native = project_native_messages([envelope("request", "a"), message])
    assert [item.role for item in native] == ["user", "user"]
    assert native[1].content == message.content.as_content()


def test_adapter_propagates_provider_failure_without_inventing_completion() -> None:
    failure = RuntimeError("provider failed")

    class Provider:
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
            raise failure

    with pytest.raises(RuntimeError) as raised:
        NativeSessionAdapter(Provider()).complete(messages=[incoming("input")])
    assert raised.value is failure


@pytest.mark.parametrize("partial", [False, True])
def test_request_snapshot_is_pure_detached_and_matches_emitted_request(
    partial: bool,
) -> None:
    seen: dict[str, object] = {}
    ids: list[str] = []

    class Provider:
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
            seen.update(
                projection="native-session-v6",
                max_tokens=32768,
                model_family="generic",
                family_adapter_version="1",
                catalog_version="sha256:"
                + hashlib.sha256(
                    json.dumps(
                        tools, ensure_ascii=True, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest(),
                messages=list(messages_payload(messages)),
                role=role,
                tools=tools,
                response_format=response_format,
            )
            return ModelCompletion(
                message=ProviderMessage("assistant", "Answer"),
                model="model",
                provider="provider",
                role=role,
                prompt_tokens=1,
                completion_tokens=2,
            )

    def new_id() -> str:
        ids.append("output")
        return "output"

    adapter = NativeSessionAdapter(Provider(), id_factory=new_id)
    messages = [envelope("request", "a", "b"), incoming("steering"), datum("A", "a")]
    if not partial:
        messages.append(datum("B", "b"))
    canonical = [message.as_dict() for message in messages]
    tools = [
        MappingProxyType(
            {
                "name": "check",
                "parameters": {"required": ("value",)},
            }
        )
    ]
    response_format = MappingProxyType(
        {"type": "json_schema", "json_schema": {"schema": {"required": ("answer",)}}}
    )
    snapshot = adapter.request_snapshot(
        messages=messages, role="research", tools=tools, response_format=response_format
    )
    assert seen == {}
    assert ids == []
    assert snapshot["messages"] == list(
        messages_payload(project_native_messages(messages))
    )
    assert snapshot["role"] == "research"
    assert snapshot["projection"] == "native-session-v6"
    assert (
        snapshot["catalog_version"]
        == "sha256:"
        + hashlib.sha256(
            b'[{"function":{"name":"check","parameters":{"required":["value"]}},"type":"function"}]'
        ).hexdigest()
    )
    expected = json.dumps(snapshot, sort_keys=True)
    adapter.complete(
        messages=messages, role="research", tools=tools, response_format=response_format
    )
    assert json.dumps({**seen, "stream": True}, sort_keys=True) == expected
    assert ids == ["output"]
    assert [message.as_dict() for message in messages] == canonical
    cast(list[dict[str, object]], snapshot["messages"])[0]["content"] = "changed"
    cast(list[dict[str, object]], snapshot["tools"])[0]["function"] = {}
    cast(dict[str, object], snapshot["response_format"])["json_schema"] = {}
    assert (
        json.dumps(
            adapter.request_snapshot(
                messages=messages,
                role="research",
                tools=tools,
                response_format=response_format,
            ),
            sort_keys=True,
        )
        == expected
    )
    assert [message.as_dict() for message in messages] == canonical


def test_request_snapshot_preserves_absent_and_empty_options() -> None:
    class Provider:
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
            raise AssertionError("snapshot must not invoke provider")

    adapter = NativeSessionAdapter(
        Provider(), id_factory=lambda: pytest.fail("snapshot must not allocate IDs")
    )
    assert adapter.request_snapshot(messages=[]) == {
        "projection": "native-session-v6",
        "stream": True,
        "max_tokens": 32768,
        "model_family": "generic",
        "family_adapter_version": "1",
        "messages": [],
        "role": "primary",
        "tools": None,
        "response_format": None,
        "catalog_version": "sha256:" + hashlib.sha256(b"null").hexdigest(),
    }
    assert adapter.request_snapshot(messages=[], tools=[], response_format={}) == {
        "projection": "native-session-v6",
        "stream": True,
        "max_tokens": 32768,
        "model_family": "generic",
        "family_adapter_version": "1",
        "messages": [],
        "role": "primary",
        "tools": [],
        "response_format": {},
        "catalog_version": "sha256:" + hashlib.sha256(b"[]").hexdigest(),
    }
    with pytest.raises(ValueError):
        adapter.request_snapshot(messages=[], response_format={"invalid": float("nan")})
    with pytest.raises(TypeError):
        adapter.request_snapshot(messages=[], tools=[{"invalid": object()}])


def test_interrupted_replay_preserves_full_reasoning_and_partial_answer():
    reasoning = "retained reasoning " * 1_000
    answer = "retained partial answer " * 1_000
    message = SessionMessage(
        "partial",
        "outgoing",
        MessageContent(
            answer,
            "agent",
            result={
                "interrupted": True,
                "reasoning": reasoning,
                "failure": {"kind": "provider_rejected"},
            },
        ),
    )
    projected = project_native_messages((message,))
    assert reasoning in projected[0].content
    assert answer in projected[0].content
