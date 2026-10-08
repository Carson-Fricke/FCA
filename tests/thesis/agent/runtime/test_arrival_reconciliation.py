"""Late durable arrivals reconcile at real root and phase provider boundaries."""

from collections.abc import Mapping, Sequence
from dataclasses import replace

import pytest

from thesis.agent.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    decode_transcript,
)
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.datum import Datum
from thesis.agent.runtime.delivery import Anchor, DeliveryError
from thesis.agent.runtime.inbox import SessionInbox
from thesis.agent.runtime.session import (
    RegisteredTool,
    SessionToolRegistry,
    SessionTurn,
    ToolExecution,
)
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.chat.contracts import (
    WORKFLOW_NODE_ROLE,
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
    SubAgentKind,
    SubAgentStatus,
    WorkflowExecution,
    WorkflowExecutionState,
)
from thesis.chat.contracts import (
    ChatMessage as DurableMessage,
)
from thesis.chat.service import ChatService
from thesis.providers.invocations import ChatMessage, ModelCompletion, Role, ToolCall
from thesis.providers.session_adapter import NativeProvider, project_native_messages


def _service(provider: NativeProvider, *, phase: bool) -> tuple[Conversations, Anchor]:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Arrival", owner_actor_id="operator")
    service = Conversations(chat=chat, provider=provider, spawn_thread=lambda _: None)
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
        )
    )
    node = service.lifecycle.spawn(
        root.workspace_id,
        root.chat_id,
        spec=SubAgentSpec(role=WORKFLOW_NODE_ROLE),
        actor_id="operator",
        workflow_execution_id="execution",
    )
    child = service.spawn(
        Anchor(root.workspace_id, root.chat_id, node.sub_agent_id),
        SubAgentSpec(role="research", phase="analysis"),
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
def test_arrival_after_post_provider_pull_matches_exact_live_replay_request(
    phase: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[tuple[ChatMessage, ...]] = []
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
        def complete(
            self,
            *,
            messages: Sequence[ChatMessage],
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
                    "Checking",
                    "model",
                    "provider",
                    role,
                    0,
                    0,
                    tool_calls=(ToolCall("call", "check", "{}"),),
                )
            assert len(requests) == 2
            assert position == 1
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
            return ModelCompletion("Finished", "model", "provider", role, 0, 0)

    service, anchor = _service(Provider(), phase=phase)
    service._registry_factory = lambda context: SessionToolRegistry(
        [
            RegisteredTool(
                {"type": "function", "function": {"name": "check"}},
                lambda arguments: ToolExecution(
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
        spec=SubAgentSpec(role="private-helper"),
        actor_id="operator",
    )
    service.deliver_session_message(
        Anchor(anchor.workspace_id, anchor.chat_id, foreign.sub_agent_id),
        SessionMessage("private", "incoming", MessageContent("Private", "user")),
        schedule=False,
    )
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
                assert durable.position == 2
            else:
                assert len(requests) == 2
                assert (
                    service.chat.read_consumed_input(
                        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
                    )
                    == 1
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
    assert [record.input_position for record in records] == [1, 2, 0, 3, 0]
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
        == 3
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
) -> None:
    requests: list[tuple[ChatMessage, ...]] = []

    class EmptyModelCompletion:
        def complete(
            self,
            *,
            messages: Sequence[ChatMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            requests.append(tuple(messages))
            return ModelCompletion(" \n", "model", "provider", role, 0, 0)

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
    assert len(canonical) == 2
    initial, notice = canonical
    assert initial.position == 1
    assert notice.direction == "incoming" and notice.content.source == "runtime"
    assert notice.position == 2
    assert notice.content.result == {"failure": result.failure}
    assert all(not message.content.requests for message in canonical)
    assert all(message.content.correlation is None for message in canonical)
    assert (
        service.chat.read_consumed_input(
            anchor.workspace_id, anchor.chat_id, anchor.schedule_id
        )
        == 0
    )
    assert len(requests) == 1
    assert requests[0][1:] == project_native_messages((initial,))
    replay = project_native_messages(canonical)
    assert all(message.role == "user" for message in replay)
    assert all(
        not message.tool_calls and message.tool_call_id is None for message in replay
    )
    invocations = service.chat.persistence.list(AgentInvocation, anchor.workspace_id)
    assert len(invocations) == 1
    invocation = invocations[0]
    assert invocation.kind is AgentInvocationKind.MODEL
    assert invocation.state is AgentInvocationState.FAILED
    assert invocation.failure == result.failure
    assert invocation.provider_request is not None
    assert invocation.request_messages
    if phase:
        child = service.lifecycle.get(anchor.workspace_id, anchor.schedule_id)
        assert child is not None and child.status is SubAgentStatus.NEEDS_INPUT
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
    assert decode_transcript(handle.transcript()) == (durable,)
    assert service.chat.get_message("workspace", incoming.message_id).body == "Original"
    assert service.chat.read_consumed_input("workspace", thread.chat_id, "") == 0
