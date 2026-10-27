"""Late durable arrivals reconcile at real root and phase provider boundaries."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from threading import Event

import pytest
from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer, store_for

from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    Role,
    ToolCall,
)
from thesis_runtime.adapters.providers.session_adapter import (
    NativeProvider,
    NativeSessionAdapter,
    project_native_messages,
)
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.datum import Datum
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    decode_transcript,
)
from thesis_runtime.contracts.output import OutputDelta
from thesis_runtime.contracts.records import (
    WORKFLOW_NODE_ROLE,
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
    SubAgentKind,
    SubAgentStatus,
    WorkflowExecution,
    WorkflowExecutionState,
)
from thesis_runtime.contracts.records import (
    ChatMessage as DurableMessage,
)
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.delivery import Anchor, DeliveryError
from thesis_runtime.conversations.inbox import SessionInbox
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.sessions.session import SessionTurn
from thesis_runtime.subagents.specs import SubAgentSpec
from thesis_runtime.tools.registry import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)


def _service(
    provider: NativeProvider, *, phase: bool, cosmos: bool = False
) -> tuple[Conversations, Anchor]:
    chat = ChatService(store_for(LimitedContainer()) if cosmos else None)
    thread = chat.create_chat("workspace", title="Arrival", owner_actor_id="operator")
    service = Conversations(
        chat=chat, provider=NativeSessionAdapter(provider), spawn_thread=lambda _: None
    )
    root = Anchor("workspace", thread.chat_id)
    if not phase:
        return service, root
    chat.persistence.save(
        WorkflowExecution(
            workspace_id=root.workspace_id,
            created_at=thread.created_at,
            created_by="operator",
            execution_id="execution",
            chat_id=root.chat_id,
            trigger_message_id="trigger",
            workflow_id="research",
            workflow_version="1.0.0",
            requested_by="operator",
            state=WorkflowExecutionState.RUNNING,
            conversation_id=root.chat_id,
            root_conversation_id=root.chat_id,
            parent_conversation_id=None,
        )
    )
    node = service.lifecycle.spawn(
        root.workspace_id,
        root.chat_id,
        spec=SubAgentSpec(role=WORKFLOW_NODE_ROLE, allowed_tools=("check",)),
        actor_id="operator",
        workflow_execution_id="execution",
    )
    child = service.spawn(
        Anchor(root.workspace_id, root.chat_id, node.sub_agent_id),
        SubAgentSpec(role="research", phase="analysis", allowed_tools=("check",)),
        "Initial",
        SubAgentKind.CONVERSATION,
        phase_executor=True,
        attribution={
            "workflow_execution_id": "execution",
            "phase_attempt_id": "attempt",
        },
    )
    return service, Anchor(root.workspace_id, root.chat_id, child.conversation_id)


@pytest.mark.parametrize("phase", [False, True], ids=["root", "phase"])
@pytest.mark.parametrize("cosmos", [False, True], ids=["memory", "cosmos"])
def test_steering_closes_generation_retains_reasoning_and_replays_exact_context(
    phase: bool, cosmos: bool
) -> None:
    requests: list[tuple[ProviderMessage, ...]] = []
    closed: list[bool] = []
    dispatched: list[object] = []
    thoughts = "Reasoning before steering \U0001f9e0"

    class Provider:
        streaming = True

        def stream(self, *, messages, role="primary", **kwargs):
            requests.append(tuple(messages))
            if len(requests) == 1:
                try:
                    yield OutputDelta("reasoning", thoughts)
                    service.deliver_session_message(
                        anchor,
                        SessionMessage(
                            "steer", "incoming", MessageContent("New direction", "user")
                        ),
                        schedule=False,
                    )
                    # Cross the cooperative input-check interval without model
                    # completion; the next delta must close this generation.
                    Event().wait(0.26)
                    yield OutputDelta("reasoning", " and the received tail")
                    pytest.fail("obsolete generation continued after steering")
                finally:
                    closed.append(True)
            else:
                assert len(requests) == 2
                assert closed == [True]
                assert (
                    service.chat.read_consumed_input(
                        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
                    )
                    == 0
                )
                canonical = decode_transcript(service.transcript(anchor))
                assert tuple(messages[1:]) == project_native_messages(canonical)
                assert any(thoughts in message.content for message in messages)
                assert any("New direction" in message.content for message in messages)
                yield ModelCompletion(
                    message=ProviderMessage("assistant", "Steered answer"),
                    model="model",
                    provider="provider",
                    role=role,
                    prompt_tokens=0,
                    completion_tokens=0,
                )

    service, anchor = _service(Provider(), phase=phase, cosmos=cosmos)

    def check(arguments, context):
        dispatched.append(arguments)
        return ToolExecution(ok=True)

    service._registry_factory = lambda _: SessionToolRegistry(
        [RegisteredTool({"name": "check"}, check)]
    )
    if not phase:
        service.root(anchor.workspace_id, anchor.chat_id).send(
            "Initial", "operator", schedule=False
        )
    result = service.run_node_turn(
        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
    )
    assert result.status == "completed" and result.content == "Steered answer"
    assert dispatched == []
    canonical = decode_transcript(service.transcript(anchor))
    partial = canonical[-2]
    assert partial.direction == "outgoing" and not partial.content.requests
    assert partial.content.result == {
        "reasoning": thoughts + " and the received tail",
        "interrupted": True,
    }
    invocations = service.chat.persistence.list(AgentInvocation, anchor.workspace_id)
    assert [item.state for item in invocations] == [
        AgentInvocationState.FAILED,
        AgentInvocationState.SUCCEEDED,
    ]
    assert invocations[0].failure is not None
    assert invocations[0].failure["kind"] == "operator_steering"
    assert any(
        ref["message_id"] == partial.message_id
        for ref in invocations[1].request_messages
    )
    assert (
        service.chat.read_consumed_input(
            anchor.workspace_id, anchor.chat_id, anchor.schedule_id
        )
        == canonical[1].position
    )
    if phase:
        assert (
            service.chat.get_execution(anchor.workspace_id, "execution").state
            is WorkflowExecutionState.RUNNING
        )


@pytest.mark.parametrize("phase", [False, True], ids=["root", "phase"])
@pytest.mark.parametrize("cosmos", [False, True], ids=["memory", "cosmos"])
def test_arrival_after_post_provider_pull_matches_exact_live_replay_request(
    phase: bool, cosmos: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[tuple[ProviderMessage, ...]] = []
    boundary_records: list[SessionMessage] = []
    outputs: list[str] = []
    late = SessionMessage(
        "late-before-output", "incoming", MessageContent("Late steering", "user")
    )
    datums = (
        Datum("first", text="First card", structured={"value": 1}),
        Datum("second", text="Second card", structured={"value": 2}),
    )

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
            requests.append(tuple(messages))
            position = service.chat.read_consumed_input(
                anchor.workspace_id, anchor.chat_id, anchor.schedule_id
            )
            if len(requests) == 1:
                assert position == 0
                return ModelCompletion(
                    message=ProviderMessage(
                        "assistant",
                        "Checking",
                        tool_calls=(ToolCall("call", "check", "{}"),),
                    ),
                    model="model",
                    provider="provider",
                    role=role,
                    prompt_tokens=0,
                    completion_tokens=0,
                )
            assert len(requests) == 2
            assert position == initial_position
            assert len(outputs) == 1
            boundary_records.extend(decode_transcript(service.transcript(anchor)))
            assert messages[0].role == "system"
            assert tuple(messages[1:]) == project_native_messages(boundary_records)
            assert [message.direction for message in boundary_records] == [
                "incoming",
                "incoming",
                "outgoing",
                "incoming",
            ]
            assert boundary_records[1].message_id == late.message_id
            return ModelCompletion(
                message=ProviderMessage("assistant", "Finished"),
                model="model",
                provider="provider",
                role=role,
                prompt_tokens=0,
                completion_tokens=0,
            )

    service, anchor = _service(Provider(), phase=phase, cosmos=cosmos)
    service._registry_factory = lambda context: SessionToolRegistry(
        [
            RegisteredTool(
                {"name": "check"},
                lambda arguments, context: ToolExecution(
                    ok=True,
                    text="Full outcome",
                    structured={"answer": 42},
                    datums=datums,
                ),
            )
        ]
    )
    if not phase:
        service.root(anchor.workspace_id, anchor.chat_id).send(
            "Initial", "operator", schedule=False
        )
    # A different anchor must never expand the reconciliation's retained scope.
    foreign = service.lifecycle.spawn(
        anchor.workspace_id,
        anchor.chat_id,
        spec=SubAgentSpec(role="private-helper", allowed_tools=("check",)),
        actor_id="operator",
    )
    service.deliver_session_message(
        Anchor(anchor.workspace_id, anchor.chat_id, foreign.sub_agent_id),
        SessionMessage("private", "incoming", MessageContent("Private", "user")),
        schedule=False,
    )
    initial_position = service.transcript(anchor)[0].input_position
    persist_turn = service._persist_turn

    def inject_before_output(
        context: ToolContext,
        turn: SessionTurn,
        *,
        activity: str | None = None,
    ) -> tuple[DurableMessage, ...]:
        if turn.message.direction == "outgoing":
            if not outputs:
                assert len(requests) == 1
                assert len(service.transcript(anchor)) == 1
                assert (
                    service.chat.read_consumed_input(
                        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
                    )
                    == 0
                )
                durable = service.deliver_session_message(anchor, late, schedule=False)
                assert durable.input_position > initial_position
            else:
                assert len(requests) == 2
                assert (
                    service.chat.read_consumed_input(
                        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
                    )
                    == initial_position
                )
            outputs.append(turn.message.message_id)
        return persist_turn(context, turn, activity=activity)

    monkeypatch.setattr(service, "_persist_turn", inject_before_output)
    result = service.run_node_turn(
        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
    )
    assert result.status == "completed", result.content
    records = service.transcript(anchor)
    canonical = decode_transcript(records)
    assert canonical[:-1] == tuple(boundary_records)
    assert [record.body for record in records] == [
        "Initial",
        "Late steering",
        "Checking",
        "Full outcome",
        "Finished",
    ]
    positions = [row.input_position for row in records]
    assert positions[0] == initial_position < positions[1] < positions[3]
    assert positions[2] == positions[4] == 0
    assert requests[1][1:] == project_native_messages(canonical[:-1])
    outcomes = [message for message in canonical if message.content.source == "tool"]
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.content.correlation == ToolCorrelation(outputs[0], "call", "check")
    assert outcome.content.text == "Full outcome"
    assert outcome.content.result is not None
    assert outcome.content.result["ok"] is True
    assert outcome.content.result["structured"] == {"answer": 42}
    serialized_result = outcome.content.as_dict()["result"]
    assert isinstance(serialized_result, Mapping)
    assert serialized_result["datums"] == [datum.as_dict() for datum in datums]
    assert records[3].data is not None
    assert "sessionMessage" in records[3].data
    assert (
        service.chat.read_consumed_input(
            anchor.workspace_id, anchor.chat_id, anchor.schedule_id
        )
        == records[3].input_position
    )


def test_reconciliation_reorders_known_messages_without_reviving_consumed_input() -> (
    None
):
    initial = SessionMessage(
        "initial", "incoming", MessageContent("Initial", "user"), 1
    )
    late = SessionMessage("late", "incoming", MessageContent("Late", "user"), 2)
    output = SessionMessage("output", "outgoing", MessageContent("Answer", "agent"))
    instruction = SessionMessage(
        "bootstrap", "instruction", MessageContent("Policy", "instruction")
    )
    inbox = SessionInbox((instruction, initial), pending_ids={initial.message_id})
    inbox.record_output(output)
    inbox.acknowledge((initial,))
    inbox.deliver(late)
    assert inbox.transcript() == (instruction, initial, output, late)
    inbox.reconcile((initial, late, output))
    assert inbox.snapshot() == ((instruction, initial, late, output), (late,))
    inbox.reconcile((initial, late, output))
    assert inbox.pending() == (late,)


@pytest.mark.parametrize("phase", [False, True], ids=["root", "phase"])
def test_empty_native_completion_interrupts_without_ack_or_workflow_failure(
    phase: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("thesis_runtime.sessions.recovery.sleep", lambda _: None)
    requests: list[tuple[ProviderMessage, ...]] = []

    class EmptyModelCompletion:
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
            requests.append(tuple(messages))
            return ModelCompletion(
                message=ProviderMessage("assistant", " \n"),
                model="model",
                provider="provider",
                role=role,
                prompt_tokens=0,
                completion_tokens=0,
            )

    service, anchor = _service(EmptyModelCompletion(), phase=phase)
    faults: list[Exception] = []
    service._on_fault = lambda record, error: faults.append(error)
    if not phase:
        service.root(anchor.workspace_id, anchor.chat_id).send(
            "Initial", "operator", schedule=False
        )
    result = service.run_node_turn(
        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
    )
    assert result.status == "interrupted"
    assert result.turns == ()
    assert result.failure is not None
    assert result.failure["kind"] == "provider_rejected"
    assert not faults
    canonical = decode_transcript(service.transcript(anchor))
    assert len(canonical) == 5
    initial, *_, notice = canonical
    assert initial.position == 1
    assert notice.direction == "incoming" and notice.content.source == "runtime"
    assert notice.position == 5
    assert notice.content.result is not None
    assert notice.content.result["failure"] == result.failure
    assert "continuation" in notice.content.result
    assert all(not message.content.requests for message in canonical)
    assert all(message.content.correlation is None for message in canonical)
    assert (
        service.chat.read_consumed_input(
            anchor.workspace_id, anchor.chat_id, anchor.schedule_id
        )
        == 0
    )
    assert len(requests) == 4
    assert requests[0][1:] == project_native_messages((initial,))
    replay = project_native_messages(canonical)
    assert all(message.role == "user" for message in replay)
    assert all(
        not message.tool_calls and message.tool_call_id is None for message in replay
    )
    invocations = service.chat.persistence.list(AgentInvocation, anchor.workspace_id)
    assert len(invocations) == 4
    invocation = invocations[0]
    assert invocation.kind is AgentInvocationKind.MODEL
    assert invocation.state is AgentInvocationState.FAILED
    assert invocation.failure == result.failure
    assert invocation.provider_request is not None
    assert invocation.request_messages
    if phase:
        child = service.lifecycle.get(anchor.workspace_id, anchor.schedule_id)
        assert child is not None and child.status is SubAgentStatus.RUNNING
        assert (
            service.chat.get_execution(anchor.workspace_id, "execution").state.value
            == "running"
        )


def test_real_delivery_rejects_same_id_changed_content_without_overwrite() -> None:
    service = Conversations(chat=ChatService(), spawn_thread=lambda _: None)
    thread = service.chat.create_chat(
        "workspace", title="Identity", owner_actor_id="operator"
    )
    handle = service.root("workspace", thread.chat_id)
    incoming = SessionMessage("same-id", "incoming", MessageContent("Original", "user"))
    durable = handle.deliver_session_message(incoming, schedule=False)
    assert handle.deliver_session_message(incoming, schedule=False) == durable
    with pytest.raises(DeliveryError, match="different content"):
        handle.deliver_session_message(
            replace(incoming, content=MessageContent("Changed", "user")), schedule=False
        )
    assert decode_transcript(handle.transcript()) == decode_transcript((durable,))
    assert service.chat.get_message("workspace", incoming.message_id).body == "Original"
    assert service.chat.read_consumed_input("workspace", thread.chat_id, "") == 0
