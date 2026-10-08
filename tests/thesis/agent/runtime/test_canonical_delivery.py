"""The public incoming port owns durable identity, ordering, and live delivery."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

import pytest

from thesis.agent.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    from_record,
)
from thesis.agent.runtime.activity import Delivery, decode_activity, is_external_input
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.conversation import Conversation, Conversations
from thesis.agent.runtime.datum import Datum
from thesis.agent.runtime.delivery import Anchor, DeliveryError, DeliveryService
from thesis.agent.runtime.session import (
    RegisteredTool,
    SessionToolRegistry,
    SessionTurn,
    ToolExecution,
)
from thesis.chat.contracts import MessageRole
from thesis.chat.service import ChatService
from thesis.providers.invocations import (
    ChatMessage,
    ModelCompletion,
    ProviderUnreachableError,
    Role,
    ToolCall,
)
from thesis.providers.session_adapter import project_native_messages


def setup_service() -> tuple[Conversations, Anchor, list[Callable[[], None]]]:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Canonical", owner_actor_id="operator")
    workers: list[Callable[[], None]] = []

    class Provider:
        def complete(
            self,
            *,
            messages: Sequence[ChatMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            return ModelCompletion("Finished", "model", "provider", role, 0, 0)

    return (
        Conversations(chat=chat, provider=Provider(), spawn_thread=workers.append),
        Anchor("workspace", thread.chat_id),
        workers,
    )


@pytest.mark.parametrize(
    "source,role",
    [
        ("user", MessageRole.USER),
        ("tool", MessageRole.TOOL),
        ("runtime", MessageRole.SYSTEM),
    ],
)
def test_public_port_returns_durable_identity_before_wake(
    source: str, role: MessageRole
) -> None:
    service, anchor, workers = setup_service()
    incoming = SessionMessage(
        "message-canonical",
        "incoming",
        MessageContent("Raw source text", source),
        position=99,
    )
    durable = service.deliver_session_message(anchor, incoming, role=role)
    record = service.chat.get_message(anchor.workspace_id, durable.message_id)
    assert durable == from_record(record)
    assert durable.message_id == record.message_id == incoming.message_id
    assert durable.position == record.input_position == 1
    assert record.body == "Raw source text"
    assert isinstance(decode_activity(record), Delivery)
    assert is_external_input(record)
    assert len(workers) == 1
    assert service.deliver_session_message(anchor, incoming, role=role) == durable
    assert len(service.transcript(anchor)) == len(workers) == 1
    assert (
        service.chat.read_consumed_input(anchor.workspace_id, anchor.chat_id, "") == 0
    )


def test_datum_intake_and_operator_send_share_the_writer() -> None:
    service, anchor, workers = setup_service()
    user = service.root(anchor.workspace_id, anchor.chat_id).send(
        "Operator text", "operator"
    )
    datum = Datum("example", ok=True, text="Source text", structured={"value": 3})
    receipt = service.deliver(anchor, datum)
    duplicate = service.deliver(anchor, datum)
    assert from_record(user).direction == "incoming"
    assert from_record(receipt.message).content.source == "example"
    assert (user.input_position, receipt.message.input_position) == (1, 2)
    assert duplicate.duplicate and duplicate.message == receipt.message
    assert len(service.transcript(anchor)) == 2
    assert len(workers) == 1


def test_standalone_delivery_uses_the_same_storage_boundary() -> None:
    service, anchor, _ = setup_service()
    receipt = DeliveryService(service.chat).deliver(
        anchor, Datum("example", text="Evidence")
    )
    assert from_record(receipt.message).message_id == receipt.message.message_id
    assert from_record(receipt.message).position == 1


def test_identity_conflicts_fail_without_overwriting() -> None:
    service, anchor, _ = setup_service()
    incoming = SessionMessage(
        "message-one", "incoming", MessageContent("Original", "user")
    )
    service.deliver_session_message(anchor, incoming, schedule=False)
    with pytest.raises(DeliveryError, match="different content"):
        service.deliver_session_message(
            anchor, replace(incoming, content=MessageContent("Changed", "user"))
        )
    other = service.chat.create_chat(
        "workspace", title="Other", owner_actor_id="operator"
    )
    with pytest.raises(DeliveryError, match="another conversation"):
        service.deliver_session_message(Anchor("workspace", other.chat_id), incoming)
    assert service.chat.get_message("workspace", incoming.message_id).body == "Original"


def test_incoming_observation_never_writes_another_record() -> None:
    service, anchor, _ = setup_service()
    message = service.deliver_session_message(
        anchor,
        SessionMessage("message-result", "incoming", MessageContent("Outcome", "tool")),
        schedule=False,
    )
    context = ToolContext(
        workspace_id=anchor.workspace_id,
        chat_id=anchor.chat_id,
        conversation_id=anchor.chat_id,
        actor_id="runtime",
    )
    assert service._persist_turn(context, SessionTurn(message=message)) == ()
    assert len(service.transcript(anchor)) == 1


@pytest.mark.parametrize("nested", [False, True])
def test_live_tool_result_is_one_canonical_message_with_all_datums(
    nested: bool,
) -> None:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Live", owner_actor_id="operator")
    requests: list[tuple[ChatMessage, ...]] = []
    workers: list[Callable[[], None]] = []

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
            if len(requests) == 1:
                assert chat.read_consumed_input("workspace", thread.chat_id, "") == 0
                return ModelCompletion(
                    "Checking",
                    "model",
                    "provider",
                    role,
                    0,
                    0,
                    tool_calls=(ToolCall("call-one", "check", "{}"),),
                )
            return ModelCompletion("Finished", "model", "provider", role, 0, 0)

    service = Conversations(
        chat=chat,
        provider=Provider(),
        spawn_thread=workers.append,
        registry_factory=lambda context: SessionToolRegistry(
            [
                RegisteredTool(
                    {"type": "function", "function": {"name": "check"}},
                    lambda arguments: ToolExecution(
                        ok=True,
                        text="Full outcome",
                        structured={"answer": 42},
                        datums=(
                            Datum("first", text="First"),
                            Datum("second", text="Second"),
                        ),
                    ),
                )
            ]
        ),
    )
    if nested:
        from thesis.agent.runtime.specs import SubAgentSpec

        child = service.lifecycle.spawn(
            "workspace",
            thread.chat_id,
            spec=SubAgentSpec(role="research"),
            actor_id="operator",
        )
        anchor = Anchor("workspace", thread.chat_id, child.sub_agent_id)
        service.deliver(
            anchor, None, body="Check", role=MessageRole.USER, idempotency_key="initial"
        )
    else:
        anchor = Anchor("workspace", thread.chat_id)
        service.root("workspace", thread.chat_id).send("Check", "operator")
    workers[0]()
    records = service.transcript(anchor)
    assert len(records) == 4
    messages = tuple(from_record(record) for record in records)
    output, result = messages[1:3]
    assert result.content.correlation == ToolCorrelation(
        output.message_id, "call-one", "check"
    )
    assert result.position == 2
    assert result.content.result is not None
    assert result.content.result["structured"] == {"answer": 42}
    datums = result.content.result["datums"]
    assert isinstance(datums, tuple) and len(datums) == 2
    assert records[2].data is not None
    assert records[2].data["activityKind"] == "delivery"
    assert requests[1][1:] == project_native_messages(messages[:3])
    assert (
        chat.read_consumed_input("workspace", thread.chat_id, anchor.schedule_id) == 2
    )
    assert len(workers) == 1


def test_provider_failure_does_not_ack_initial_input() -> None:
    service, anchor, _ = setup_service()

    class Provider:
        def complete(
            self,
            *,
            messages: Sequence[ChatMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            raise ProviderUnreachableError("Unavailable")

    service = Conversations(
        chat=service.chat, provider=Provider(), spawn_thread=lambda callback: None
    )
    service.root(anchor.workspace_id, anchor.chat_id).send("Keep pending", "operator")
    assert (
        service.run_node_turn(anchor.workspace_id, anchor.chat_id, "").status
        == "interrupted"
    )
    assert (
        service.chat.read_consumed_input(anchor.workspace_id, anchor.chat_id, "") == 0
    )
    notification = from_record(service.transcript(anchor)[-1])
    assert (
        notification.direction == "incoming"
        and notification.content.source == "runtime"
    )


def test_runtime_tool_start_is_activity_and_result_is_incoming() -> None:
    service, anchor, workers = setup_service()
    context = ToolContext(
        workspace_id=anchor.workspace_id,
        chat_id=anchor.chat_id,
        conversation_id=anchor.chat_id,
        actor_id="runtime",
    )
    execution = ToolExecution(
        ok=False, text="Validation failed", failure={"kind": "validation"}
    )
    assert (
        service.execute_runtime_tool(
            context, "validate", {"code": "source"}, lambda: execution
        )
        == execution
    )
    start, outcome = service.transcript(anchor)
    assert start.input_position == 0 and not is_external_input(start)
    assert outcome.input_position == 1 and is_external_input(outcome)
    assert from_record(outcome).direction == "incoming"
    assert from_record(outcome).content.correlation is None
    assert outcome.tool_result is not None and outcome.tool_result["ok"] is False
    assert not workers


def test_root_send_can_defer_wake_until_steering_is_installed() -> None:
    service, anchor, workers = setup_service()
    message = service.root(anchor.workspace_id, anchor.chat_id).send(
        "Steer next", "operator", intent_epoch=3, schedule=False
    )
    assert from_record(message).direction == "incoming"
    assert message.intent_epoch == 3 and message.input_position == 1
    assert not workers
    service.schedule_input(message)
    assert len(workers) == 1
    workers[0]()
    assert (
        service.chat.read_consumed_input(anchor.workspace_id, anchor.chat_id, "") == 1
    )


def test_nested_send_cannot_defer_answer_admission_wake() -> None:
    from thesis.agent.runtime.specs import SubAgentSpec

    service, root, workers = setup_service()
    child = service.lifecycle.spawn(
        root.workspace_id,
        root.chat_id,
        spec=SubAgentSpec(role="helper"),
        actor_id="operator",
    )
    conversation = Conversation(
        service, Anchor(root.workspace_id, root.chat_id, child.sub_agent_id)
    )
    with pytest.raises(DeliveryError, match="only root"):
        conversation.send("Answer", "operator", schedule=False)
    assert not workers
    assert not service.transcript(
        Anchor(root.workspace_id, root.chat_id, child.sub_agent_id)
    )


def test_completed_phase_return_to_workflow_record_never_starts_a_turn() -> None:
    from thesis.agent.runtime.specs import SubAgentSpec
    from thesis.chat.contracts import WORKFLOW_NODE_ROLE, WorkflowExecution

    service, root, workers = setup_service()
    thread = service.chat.get_chat(root.workspace_id, root.chat_id)
    service.chat.persistence.save(
        WorkflowExecution(
            workspace_id=root.workspace_id,
            created_at=thread.created_at,
            created_by="operator",
            execution_id="execution",
            chat_id=root.chat_id,
            trigger_message_id="trigger",
            workflow_id="workflow",
            workflow_version="1.0.0",
            requested_by="operator",
        )
    )
    workflow = service.lifecycle.spawn(
        root.workspace_id,
        root.chat_id,
        spec=SubAgentSpec(role=WORKFLOW_NODE_ROLE),
        actor_id="operator",
    )
    phase = service.lifecycle.spawn(
        root.workspace_id,
        root.chat_id,
        spec=SubAgentSpec(role="phase", phase="creation"),
        parent_sub_agent_id=workflow.sub_agent_id,
        actor_id="operator",
        phase_executor=True,
        workflow_execution_id="execution",
        phase_attempt_id="attempt",
    )
    completed = service.lifecycle.complete(
        root.workspace_id, phase.sub_agent_id, result={"accepted": True}
    )
    assert completed is not None
    anchor = Anchor(root.workspace_id, root.chat_id, workflow.sub_agent_id)
    receipt = service.delivery.deliver_child_result(anchor, completed)
    assert receipt.message.input_position == 1
    assert from_record(receipt.message).direction == "incoming"
    assert not workers
    assert not service.in_flight(root.workspace_id, root.chat_id, workflow.sub_agent_id)
    assert (
        service.chat.read_consumed_input(
            root.workspace_id, root.chat_id, workflow.sub_agent_id
        )
        == 0
    )


def test_midturn_terminal_input_stays_durable_but_unconsumed() -> None:
    from thesis.agent.runtime.specs import SubAgentSpec
    from thesis.chat.contracts import SubAgentStatus

    chat = ChatService()
    thread = chat.create_chat("workspace", title="Terminal", owner_actor_id="operator")
    workers: list[Callable[[], None]] = []
    requests: list[tuple[ChatMessage, ...]] = []

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
            service.deliver_session_message(
                anchor,
                SessionMessage(
                    "late-steering",
                    "incoming",
                    MessageContent("Accepted during model call", "user"),
                ),
            )
            service.lifecycle.complete(
                anchor.workspace_id,
                anchor.sub_agent_id or "",
                result={"accepted": True},
            )
            return ModelCompletion("Finished", "model", "provider", role, 0, 0)

    service = Conversations(chat=chat, provider=Provider(), spawn_thread=workers.append)
    child = service.lifecycle.spawn(
        "workspace",
        thread.chat_id,
        spec=SubAgentSpec(role="helper"),
        actor_id="operator",
    )
    anchor = Anchor("workspace", thread.chat_id, child.sub_agent_id)
    service.deliver_session_message(
        anchor, SessionMessage("initial", "incoming", MessageContent("Initial", "user"))
    )
    workers[0]()
    assert len(requests) == len(workers) == 1
    assert (
        chat.read_consumed_input("workspace", thread.chat_id, child.sub_agent_id) == 1
    )
    late = chat.get_message("workspace", "late-steering")
    assert late.body == "Accepted during model call" and late.input_position == 2
    assert not service.in_flight("workspace", thread.chat_id, child.sub_agent_id)
    assert not service._workers._states
    current = service.lifecycle.get("workspace", child.sub_agent_id)
    assert current is not None and current.status is SubAgentStatus.COMPLETED
    service.schedule_input(late)
    assert len(workers) == 1
    assert (
        chat.read_consumed_input("workspace", thread.chat_id, child.sub_agent_id) == 1
    )


def test_storage_rejects_mismatched_canonical_identity() -> None:
    service, anchor, _ = setup_service()
    message = SessionMessage(
        "canonical-id", "incoming", MessageContent("Evidence", "user")
    )
    with pytest.raises(ValueError, match="identity"):
        service.chat.create_message(
            anchor.workspace_id,
            anchor.chat_id,
            actor_id="operator",
            body="Evidence",
            message_id="other-id",
            data={"sessionMessage": message.as_dict()},
        )
    assert service.transcript(anchor) == ()


def test_midturn_input_keeps_arrival_order_and_is_consumed_next_boundary() -> None:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Midturn", owner_actor_id="operator")
    workers: list[Callable[[], None]] = []
    requests: list[tuple[ChatMessage, ...]] = []

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
            if len(requests) == 1:
                return ModelCompletion(
                    "Checking",
                    "model",
                    "provider",
                    role,
                    0,
                    0,
                    tool_calls=(ToolCall("call", "check", "{}"),),
                )
            assert chat.read_consumed_input("workspace", thread.chat_id, "") == 1
            return ModelCompletion("Finished", "model", "provider", role, 0, 0)

    def handler(arguments: Mapping[str, object]) -> ToolExecution:
        service.root("workspace", thread.chat_id).send("Fresh steering", "operator")
        return ToolExecution(ok=True, text="Checked")

    service = Conversations(
        chat=chat,
        provider=Provider(),
        spawn_thread=workers.append,
        registry_factory=lambda context: SessionToolRegistry(
            [
                RegisteredTool(
                    {"type": "function", "function": {"name": "check"}}, handler
                )
            ]
        ),
    )
    service.root("workspace", thread.chat_id).send("Initial", "operator")
    workers[0]()
    records = service.transcript(Anchor("workspace", thread.chat_id))
    assert [record.body for record in records] == [
        "Initial",
        "Checking",
        "Fresh steering",
        "Checked",
        "Finished",
    ]
    assert [record.input_position for record in records] == [1, 0, 2, 3, 0]
    assert requests[1][1:] == project_native_messages(
        tuple(from_record(record) for record in records[:-1])
    )
    assert chat.read_consumed_input("workspace", thread.chat_id, "") == 3
    assert len(requests) == 2 and len(workers) == 1


def test_persistence_failure_never_publishes_or_wakes() -> None:
    from thesis.chat.contracts import ChatMessage as DurableMessage
    from thesis.chat.service import InMemoryChatPersistence, RecordT

    class FailingPersistence(InMemoryChatPersistence):
        def save(self, record: RecordT) -> RecordT:
            if isinstance(record, DurableMessage):
                raise OSError("storage unavailable")
            return super().save(record)

    chat = ChatService(persistence=FailingPersistence())
    thread = chat.create_chat("workspace", title="Failure", owner_actor_id="operator")
    workers: list[Callable[[], None]] = []
    service = Conversations(chat=chat, spawn_thread=workers.append)
    with pytest.raises(OSError, match="storage unavailable"):
        service.deliver_session_message(
            Anchor("workspace", thread.chat_id),
            SessionMessage("failed-id", "incoming", MessageContent("Evidence", "user")),
        )
    assert not workers
    assert not chat.list_messages("workspace", thread.chat_id)


def test_cancelled_child_retains_late_delivery_without_resurrection() -> None:
    from thesis.agent.runtime.specs import SubAgentSpec
    from thesis.chat.contracts import SubAgentStatus

    service, root, workers = setup_service()
    child = service.lifecycle.spawn(
        root.workspace_id,
        root.chat_id,
        spec=SubAgentSpec(role="research"),
        actor_id="operator",
    )
    service.lifecycle.cancel(root.workspace_id, child.sub_agent_id)
    anchor = Anchor(root.workspace_id, root.chat_id, child.sub_agent_id)
    message = SessionMessage(
        "late-result", "incoming", MessageContent("Late evidence", "tool")
    )
    durable = service.deliver_session_message(anchor, message)
    assert durable.position == 1
    assert service.deliver_session_message(anchor, message) == durable
    current = service.lifecycle.get(root.workspace_id, child.sub_agent_id)
    assert current is not None and current.status is SubAgentStatus.CANCELLED
    assert len(service.transcript(anchor)) == 1
    assert not workers
