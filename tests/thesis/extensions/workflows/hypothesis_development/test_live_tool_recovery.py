"""Durable phase transcripts and node-local infrastructure recovery."""

from __future__ import annotations

import json
import ssl
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from tests.thesis.extensions.workflows.hypothesis_development._support import (
    _only_active_phase,
)

from thesis.agent.harness_tools import HarnessToolProvider
from thesis.extensions.tools.registry import build_session_registry
from thesis.integrations.mcp import HarnessMcpClient, HarnessMcpTransportError
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
from thesis_runtime.contracts.datum import Datum
from thesis_runtime.contracts.messages import (
    MessageContent,
    ToolCorrelation,
    decode_transcript,
    from_record,
)
from thesis_runtime.contracts.records import MessageRole, SubAgentRecord, SubAgentStatus
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.execution.events import ExecutionEventType
from thesis_runtime.subagents.lifecycle import SubAgentLifecycle
from thesis_runtime.subagents.policy import AnsweringActor, OperatorActorKind
from thesis_runtime.subagents.store import PersistenceSubAgentStore
from thesis_runtime.tools.registry import (
    RegisteredTool,
    SessionPersistenceError,
    SessionToolRegistry,
    ToolExecution,
    ToolHandler,
)

from ._support import (
    OWNER,
    WORKSPACE,
    _discovery_envelope,
    _kit,
    _node_record,
    _request,
)
from .test_agent_first_loop import _control, _node_messages, _runner

_CALL = ToolCall("study-call", "validate_study", '{"source":"study"}')


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


def _registry(handler: ToolHandler) -> SessionToolRegistry:
    return SessionToolRegistry(
        [
            RegisteredTool(
                {
                    "name": _CALL.name,
                    "parameters": {"type": "object"},
                },
                handler,
            )
        ]
    )


def test_live_tool_transcript_survives_model_ssl_and_replays_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("thesis_runtime.sessions.recovery.sleep", lambda _: None)
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    dispatched: list[Mapping[str, object]] = []

    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        messages = _node_messages(kit.chat, request.chat_id, node_id)
        envelope = messages[-1]
        assert envelope.role is MessageRole.ASSISTANT
        assert envelope.data is not None
        canonical_envelope = from_record(envelope)
        assert [request.id for request in canonical_envelope.content.requests] == [
            _CALL.id
        ]
        assert [request.name for request in canonical_envelope.content.requests] == [
            _CALL.name
        ]
        assert envelope.body == "Checking the study"
        assert envelope.data["startedAt"]
        dispatched.append(arguments)
        return ToolExecution(ok=True, text="Compiled", structured={"valid": True})

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
                durable = next(
                    row
                    for row in _node_messages(kit.chat, request.chat_id, node_id)
                    if from_record(row).content.correlation is not None
                )
                assert durable.role is MessageRole.TOOL
                assert durable.data is not None
                durable_canonical = from_record(durable)
                durable_result = durable_canonical.content.result
                assert durable_result is not None
                assert durable_result["arguments"] == {"source": "study"}
                assert durable.tool_result is not None
                assert durable.tool_result["ok"] is True
                tool_message = next(
                    message for message in messages if message.tool_call_id == _CALL.id
                )
                canonical = from_record(durable)
                assert canonical.direction == "incoming"
                assert canonical.content.source == "tool"
                assert canonical.content.text == "Compiled"
                result_request = next(
                    message
                    for message in decode_transcript(
                        _node_messages(kit.chat, request.chat_id, node_id)
                    )
                    if message.content.requests
                )
                assert canonical.content.correlation == ToolCorrelation(
                    result_request.message_id, _CALL.id, _CALL.name
                )
                assert tool_message.content == canonical.content.as_content()
            return super().complete(
                messages=messages,
                role=role,
                tools=tools,
                response_format=response_format,
            )

    provider = LiveProvider(
        completion("Checking the study", _CALL),
        *([ProviderUnreachableError("model TLS failed")] * 4),
    )
    faults: list[Exception] = []
    wakes: list[object] = []
    launch_wakes = tuple(kit.woke)
    other_messages = [
        message
        for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
        if (message.data or {}).get("subAgentId") != node_id
    ]

    def on_fault(record: SubAgentRecord, error: Exception) -> None:
        faults.append(error)
        kit.workflow.fail_execution(request, reason=str(error))

    runner = Conversations(
        chat=kit.chat,
        store=store,
        lifecycle=lifecycle,
        provider=NativeSessionAdapter(provider),
        registry_factory=lambda context: _registry(handler),
        on_fault=on_fault,
        spawn_thread=wakes.append,
    )

    result = runner.run_node_turn(WORKSPACE, request.chat_id, node_id)

    assert result.status == "interrupted"
    assert dispatched == [{"source": "study"}]
    assert faults == []
    assert wakes
    assert tuple(kit.woke) == launch_wakes
    supervisor_messages = [
        message
        for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
        if (message.data or {}).get("subAgentId") != node_id
    ]
    assert supervisor_messages[: len(other_messages)] == other_messages
    assert all(
        (from_record(m).content.result or {}).get("childRecovery")
        for m in supervisor_messages[len(other_messages) :]
    )
    assert _node_record(kit, node_id).status is SubAgentStatus.RUNNING
    assert _node_record(kit, node_id).accepts_input is True
    assert (
        kit.chat.get_execution(WORKSPACE, request.execution_id).state.value == "running"
    )
    durable = _node_messages(kit.chat, request.chat_id, node_id)
    without_recovery = [
        message
        for message in durable
        if not (from_record(message).content.result or {}).get("providerRecovery")
    ]
    assert [message.role for message in without_recovery[-3:]] == [
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.SYSTEM,
    ]
    notice = durable[-1]
    assert notice.data is not None
    assert notice.data["status"] == "continuing"
    assert notice.data["failure"] == result.failure
    assert "model TLS failed" in notice.body
    assert all(
        message.data and message.data["subAgentId"] == node_id for message in durable
    )
    events = kit.workflow.observe(WORKSPACE, request.execution_id).events
    assert not any(
        event.event_type is ExecutionEventType.SUB_AGENT_NEEDS_INPUT
        and event.payload["subAgentId"] == node_id
        for event in events
    )
    assert not any(
        event.event_type is ExecutionEventType.SUB_AGENT_FAILED for event in events
    )

    retry = Provider(completion("The earlier study compiled successfully."))
    resumed = _runner(
        kit, store, _control(kit, store, request), retry, lifecycle=lifecycle
    ).run_node_turn(WORKSPACE, request.chat_id, node_id)
    assert resumed.status == "completed"
    replay = retry.requests[0]
    envelope_index = next(i for i, message in enumerate(replay) if message.tool_calls)
    assert replay[envelope_index].tool_calls == (_CALL,)
    assert replay[envelope_index + 1].role == "tool"
    assert replay[envelope_index + 1].tool_call_id == _CALL.id
    outcome = json.loads(replay[envelope_index + 1].content)
    assert outcome["source"] == "tool"
    assert outcome["text"] == "Compiled"
    assert outcome["result"]["ok"] is True
    assert outcome["correlation"] == {
        "request_message_id": without_recovery[-3].message_id,
        "request_id": _CALL.id,
        "tool_name": _CALL.name,
    }
    assert replay[envelope_index + 1] == provider.requests[1][-1]
    assert "continuation" in json.loads(replay[-1].content)["result"]
    assert dispatched == [{"source": "study"}]
    assert _node_record(kit, node_id).status is SubAgentStatus.RUNNING
    durable = _node_messages(kit.chat, request.chat_id, node_id)
    assert (
        len(
            [
                message
                for message in durable
                if from_record(message).content.source == "tool"
            ]
        )
        == 1
    )
    assert [
        message
        for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
        if (message.data or {}).get("subAgentId") != node_id
    ] == supervisor_messages


@pytest.mark.parametrize("tool", ["request_phase_completion", "request_workflow_input"])
def test_terminal_control_transition_completes_turn_and_skips_remaining_calls(
    tmp_path: Path, tool: str
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    arguments = (
        {"completion": {"target": "model_creation", "discovery": _discovery_envelope()}}
        if tool == "request_phase_completion"
        else {"message": "Confirm the candidate.", "reason": "Operator review"}
    )
    calls = (
        ToolCall("terminal-call", tool, json.dumps(arguments)),
        ToolCall(
            "skipped-call", "request_workflow_input", '{"message":"Must not run"}'
        ),
    )
    provider = Provider(completion("Candidate ready", *calls))
    result = _runner(
        kit, store, _control(kit, store, request), provider, lifecycle=lifecycle
    ).run_node_turn(WORKSPACE, request.chat_id, node_id)

    assert result.status == "completed"
    assert len(provider.requests) == 1
    assert [turn.message.direction for turn in result.turns] == [
        "outgoing",
        "incoming",
        "incoming",
    ]
    for turn, call in zip(result.turns[1:], calls, strict=True):
        assert turn.message.content.source == "tool"
        assert turn.message.content.correlation == ToolCorrelation(
            result.turns[0].message.message_id, call.id, call.name
        )
        assert turn.message.content.text == (
            ""
            if call.id == "skipped-call"
            else "Operator input requested; this turn is paused."
            if tool == "request_workflow_input"
            else "Completion recorded; the result was delivered to the caller."
        )
    accepted = json.loads(result.turns[1].message.content.as_content())["result"]
    assert accepted["ok"] is True
    skipped = json.loads(result.turns[2].message.content.as_content())["result"]
    assert skipped["ok"] is False
    assert skipped["failure"]["kind"] == "skipped"
    assert skipped["failure"]["certainty"] == "not_started"
    assert skipped["failure"]["retry_safe"] is True
    assert _node_record(kit, node_id).status is (
        SubAgentStatus.COMPLETED
        if tool == "request_phase_completion"
        else SubAgentStatus.NEEDS_INPUT
    )
    durable = _node_messages(kit.chat, request.chat_id, node_id)
    tool_messages = [message for message in durable if message.role is MessageRole.TOOL]
    assert len(tool_messages) == 2
    correlations = [
        from_record(message).content.correlation for message in tool_messages
    ]
    assert [
        correlation.request_id if correlation is not None else None
        for correlation in correlations
    ] == [call.id for call in calls]
    assert tool_messages[1].tool_result is not None
    assert tool_messages[1].tool_result["failure"] == skipped["failure"]
    events = kit.workflow.observe(WORKSPACE, request.execution_id).events
    needs_input = [
        event
        for event in events
        if event.event_type is ExecutionEventType.SUB_AGENT_NEEDS_INPUT
        and event.payload.get("subAgentId") == node_id
    ]
    assert len(needs_input) == (0 if tool == "request_phase_completion" else 1)
    assert _only_active_phase(kit, WORKSPACE, request.execution_id) == (
        "model_creation" if tool == "request_phase_completion" else "discovery"
    )


def test_persisted_midtool_steering_replays_after_the_tool_result(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    steering = "Use only the untouched daily bars."
    dispatched: list[Mapping[str, object]] = []

    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        dispatched.append(arguments)
        kit.conversations.answer(
            WORKSPACE,
            request.chat_id,
            node_id,
            body=steering,
            actor=AnsweringActor(OWNER, OperatorActorKind.HUMAN_OPERATOR),
        )
        return ToolExecution(ok=True, text="Compiled", structured={"valid": True})

    provider = Provider(completion("Checking", _CALL), completion("Using daily bars."))
    runner = Conversations(
        spawn_thread=lambda _: None,
        chat=kit.chat,
        provider=NativeSessionAdapter(provider),
        registry_factory=lambda context: _registry(handler),
    )
    result = runner.run_node_turn(WORKSPACE, request.chat_id, node_id)

    assert result.status == "completed"
    live = provider.requests[1]
    assert live[-3].tool_calls == (_CALL,)
    assert live[-2].role == "tool"
    assert live[-2].tool_call_id == _CALL.id
    steering_content = MessageContent(steering, "user")
    assert json.loads(live[-1].content) == {
        "text": steering,
        "source": "user",
        "result": None,
        "correlation": None,
    }
    assert live[-1] == ProviderMessage(
        role="user", content=steering_content.as_content()
    )
    assert (
        sum(message.content == steering_content.as_content() for message in live) == 1
    )
    durable = _node_messages(kit.chat, request.chat_id, node_id)
    assert [message.role for message in durable[-4:]] == [
        MessageRole.ASSISTANT,
        MessageRole.USER,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    canonical = decode_transcript(durable)
    assert [message.direction for message in canonical[-4:]] == [
        "outgoing",
        "incoming",
        "incoming",
        "outgoing",
    ]
    assert live[1:] == project_native_messages(canonical[:-1])

    replay_provider = Provider(completion("Continuing with daily bars."))
    replay_runner = Conversations(
        spawn_thread=lambda _: None,
        chat=kit.chat,
        provider=NativeSessionAdapter(replay_provider),
        registry_factory=lambda context: _registry(handler),
    )
    assert (
        replay_runner.run_node_turn(WORKSPACE, request.chat_id, node_id).status
        == "completed"
    )
    replay = replay_provider.requests[0]
    index = next(i for i, message in enumerate(replay) if message.tool_calls)
    assert replay[index].tool_calls == (_CALL,)
    assert replay[index + 1].role == "tool"
    assert replay[index + 1].tool_call_id == _CALL.id
    assert replay[index + 2] == live[-1]
    assert (
        sum(message.content == steering_content.as_content() for message in replay) == 1
    )
    assert replay[0] == live[0]
    assert replay[1:] == project_native_messages(canonical)
    assert replay[:-1] == live
    assert dispatched == [{"source": "study"}]


def test_multiple_typed_datums_persist_at_node_and_replay_as_one_tool_result(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    datums = (
        Datum(
            "study_validation",
            structured={"valid": True, "modelId": "momentum"},
            text="The study compiled.",
            summary="Compiled study",
        ),
        Datum(
            "study_artifact",
            structured={"path": "studies/momentum", "sha256": "a" * 64},
            text="The compiled artifact is available.",
            summary="Study artifact",
        ),
    )
    dispatched: list[Mapping[str, object]] = []

    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        dispatched.append(arguments)
        return ToolExecution(ok=True, datums=datums)

    provider = Provider(
        completion("Validating", _CALL), completion("Validation ready.")
    )
    runner = Conversations(
        spawn_thread=lambda _: None,
        chat=kit.chat,
        provider=NativeSessionAdapter(provider),
        registry_factory=lambda context: _registry(handler),
    )
    assert (
        runner.run_node_turn(WORKSPACE, request.chat_id, node_id).status == "completed"
    )
    tool_messages = [
        message
        for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
        if message.role is MessageRole.TOOL
    ]
    assert len(tool_messages) == 1
    message = tool_messages[0]
    assert message.data is not None
    assert message.data["subAgentId"] == node_id
    canonical = from_record(message)
    result_payload = canonical.content.result
    assert result_payload is not None
    assert result_payload["arguments"] == {"source": "study"}
    assert result_payload["invocationId"]
    assert canonical.direction == "incoming"
    assert canonical.content.source == "tool"
    assert canonical.content.text == ""
    outcome = json.loads(canonical.content.as_content())
    assert outcome["result"]["ok"] is True
    assert outcome["result"]["datums"] == [datum.as_dict() for datum in datums]
    request_message = next(
        item
        for item in decode_transcript(
            _node_messages(kit.chat, request.chat_id, node_id)
        )
        if item.content.requests
    )
    assert canonical.content.correlation == ToolCorrelation(
        request_message.message_id, _CALL.id, _CALL.name
    )
    assert provider.requests[1][-1].content == canonical.content.as_content()

    replay_provider = Provider(completion("Using both validation outputs."))
    replay_runner = Conversations(
        spawn_thread=lambda _: None,
        chat=kit.chat,
        provider=NativeSessionAdapter(replay_provider),
        registry_factory=lambda context: _registry(handler),
    )
    assert (
        replay_runner.run_node_turn(WORKSPACE, request.chat_id, node_id).status
        == "completed"
    )
    replay = replay_provider.requests[0]
    index = next(i for i, message in enumerate(replay) if message.tool_calls)
    assert replay[index].tool_calls == (_CALL,)
    results = [message for message in replay if message.role == "tool"]
    assert len(results) == 1
    assert replay[index + 1] == results[0]
    assert results[0].tool_call_id == _CALL.id
    assert json.loads(results[0].content) == outcome
    assert results[0] == provider.requests[1][-1]
    assert dispatched == [{"source": "study"}]


def test_handler_persistence_error_is_terminal_node_fault_not_tool_failure(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    error = SessionPersistenceError("Cannot persist the study mutation")
    faults: list[tuple[SubAgentRecord, Exception]] = []

    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        raise error

    def on_fault(record: SubAgentRecord, caught: Exception) -> None:
        faults.append((record, caught))

    provider = Provider(completion("Validating", _CALL), completion("Must not run"))
    runner = Conversations(
        spawn_thread=lambda _: None,
        chat=kit.chat,
        provider=NativeSessionAdapter(provider),
        registry_factory=lambda context: _registry(handler),
        on_fault=on_fault,
    )
    result = runner.run_node_turn(WORKSPACE, request.chat_id, node_id)

    assert result.status == "failed"
    assert result.content == str(error)
    assert len(provider.requests) == 1
    assert len(faults) == 1
    assert faults[0][1] is error
    assert faults[0][0].sub_agent_id == node_id
    assert faults[0][0].status is SubAgentStatus.FAILED
    record = _node_record(kit, node_id)
    assert record.status is SubAgentStatus.FAILED
    assert record.accepts_input is False
    assert record.result == {"failure": str(error)}
    durable = _node_messages(kit.chat, request.chat_id, node_id)
    assert durable[-1].role is MessageRole.ASSISTANT
    assert durable[-1].data is not None
    failed_envelope = from_record(durable[-1])
    assert [request.id for request in failed_envelope.content.requests] == [_CALL.id]
    assert not any(message.role is MessageRole.TOOL for message in durable)
    assert not any(
        (message.data or {}).get("status") == "interrupted" for message in durable
    )


def test_tool_ssl_is_durable_and_delivered_to_model_without_failing_workflow(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None

    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        raise ssl.SSLError("study TLS failed")

    provider = Provider(
        completion("Checking", _CALL), completion("Inspect before retry.")
    )
    runner = Conversations(
        spawn_thread=lambda _: None,
        chat=kit.chat,
        provider=NativeSessionAdapter(provider),
        registry_factory=lambda context: _registry(handler),
        on_fault=lambda record, error: kit.workflow.fail_execution(
            request, reason=str(error)
        ),
    )
    result = runner.run_node_turn(WORKSPACE, request.chat_id, node_id)

    assert result.status == "completed"
    model_result = provider.requests[1][-1]
    assert model_result.role == "tool"
    assert model_result.tool_call_id == _CALL.id
    outcome = json.loads(model_result.content)
    assert outcome["source"] == "tool"
    assert outcome["text"] == ""
    failure = outcome["result"]["failure"]
    assert "study TLS failed" in failure["message"]
    assert failure["certainty"] == "unknown_outcome"
    assert failure["retry_safe"] is False
    tool = next(
        message
        for message in _node_messages(kit.chat, request.chat_id, node_id)
        if message.role is MessageRole.TOOL
    )
    assert tool.tool_result is not None
    assert tool.tool_result["ok"] is False
    assert tool.tool_result["failure"] == failure
    canonical = from_record(tool)
    assert canonical.direction == "incoming"
    assert model_result.content == canonical.content.as_content()
    request_message = next(
        message
        for message in decode_transcript(
            _node_messages(kit.chat, request.chat_id, node_id)
        )
        if message.content.requests
    )
    assert canonical.content.correlation == ToolCorrelation(
        request_message.message_id, _CALL.id, _CALL.name
    )
    assert _node_record(kit, node_id).status is SubAgentStatus.RUNNING
    assert (
        kit.chat.get_execution(WORKSPACE, request.execution_id).state.value == "running"
    )


@pytest.mark.parametrize(
    "error", [ProviderUnreachableError("cancelled TLS"), RuntimeError("late fault")]
)
def test_cancellation_during_model_fault_does_not_overwrite_terminal_status(
    tmp_path: Path, error: Exception
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)

    class CancellingProvider(Provider):
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
            lifecycle.cancel(WORKSPACE, node_id)
            raise error

    faults: list[Exception] = []
    runner = _runner(
        kit,
        store,
        _control(kit, store, request),
        CancellingProvider(),
        lifecycle=lifecycle,
        on_fault=lambda record, error: faults.append(error),
    )
    runner.run_node_turn(WORKSPACE, request.chat_id, node_id)

    assert _node_record(kit, node_id).status is SubAgentStatus.CANCELLED
    assert _node_record(kit, node_id).accepts_input is False
    assert faults == []
    assert not any(
        message.role is MessageRole.SYSTEM
        for message in _node_messages(kit.chat, request.chat_id, node_id)
    )


def test_catalog_failure_interrupts_only_its_phase_and_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("thesis_runtime.sessions.recovery.sleep", lambda _: None)
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None

    class Transport:
        error: Exception | None = None

        def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
            if self.error is not None:
                raise self.error
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"tools": [{"name": "validate_study", "inputSchema": {}}]},
            }

        def notify(self, request: Mapping[str, object]) -> None:
            pass

        def close(self) -> None:
            pass

    transport = Transport()
    transport.error = HarnessMcpTransportError("catalog TLS failed")
    harness = HarnessToolProvider(HarnessMcpClient(transport))
    provider = Provider(completion("Catalog recovered."))
    faults: list[Exception] = []

    def on_fault(record: SubAgentRecord, error: Exception) -> None:
        faults.append(error)
        kit.workflow.fail_execution(request, reason=str(error))

    runner = Conversations(
        spawn_thread=lambda _: None,
        chat=kit.chat,
        provider=NativeSessionAdapter(provider),
        registry_factory=lambda context: build_session_registry(
            context=context, harness_tools=harness
        ),
        on_fault=on_fault,
    )
    try:
        result = runner.run_node_turn(WORKSPACE, request.chat_id, node_id)
        assert result.status == "interrupted"
        assert result.turns == ()
        assert provider.requests == []
        assert faults == []
        assert _node_record(kit, node_id).status is SubAgentStatus.RUNNING
        assert (
            kit.chat.get_execution(WORKSPACE, request.execution_id).state.value
            == "running"
        )
        notice = _node_messages(kit.chat, request.chat_id, node_id)[-1]
        assert notice.role is MessageRole.SYSTEM
        assert notice.data is not None
        assert notice.data["failure"] == result.failure
        assert result.failure is not None
        assert result.failure["kind"] == "transport"
        assert result.failure["executionCertainty"] == "unknown_outcome"
        assert "catalog TLS failed" in notice.body
        assert not any(
            message.role is MessageRole.SYSTEM
            and (message.data or {}).get("subAgentId") != node_id
            for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
        )
        transport.error = None
        assert (
            runner.run_node_turn(WORKSPACE, request.chat_id, node_id).status
            == "completed"
        )
        assert len(provider.requests) == 1
        assert _node_record(kit, node_id).status is SubAgentStatus.RUNNING
    finally:
        harness.close()
