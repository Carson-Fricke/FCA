"""Unified delivery, node turns, and per-node operator policy."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

import pytest

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.control import (
    TOOL_REQUEST_OPERATOR_INPUT,
    ControlToolService,
)
from thesis.agent.runtime.control_tools import control_registered_tools
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.datum import DatumType, datum_from_result
from thesis.agent.runtime.delivery import (
    DELIVERY_KEY_FIELD,
    ROOT_ANCHOR_ID,
    Anchor,
    DeliveryError,
    DeliveryService,
    NodeInputNotAccepted,
    NodeInputNotFound,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import (
    AnsweringActor,
    NodeOperatorPolicy,
    OperatorActorKind,
    OperatorPolicyDenied,
    OperatorPolicyRegistry,
)
from thesis.agent.runtime.session import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat.contracts import (
    MessageRole,
    SubAgentKind,
    SubAgentStatus,
    TranscriptItemKind,
    WorkflowExecution,
)
from thesis.chat.service import ChatService, InMemoryChatPersistence
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ModelCompletion, ToolCall

_WORKSPACE = "ws-test"
_ACTOR = "operator-1"


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


class _FakeProvider:
    def __init__(self, completions: Sequence[ModelCompletion]) -> None:
        self._completions = list(completions)
        self.calls: list[tuple[ProviderChatMessage, ...]] = []

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


def _setup() -> tuple[ChatService, str, PersistenceSubAgentStore, SubAgentLifecycle]:
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    store = PersistenceSubAgentStore(chat.persistence)
    return chat, thread.chat_id, store, SubAgentLifecycle(chat, store=store)


def _service() -> tuple[ChatService, str, PersistenceSubAgentStore]:
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    return chat, thread.chat_id, PersistenceSubAgentStore(chat.persistence)


def _spawn(
    lifecycle: SubAgentLifecycle,
    chat_id: str,
    *,
    role: str = "research",
    kind: SubAgentKind = SubAgentKind.CONVERSATION,
    parent_sub_agent_id: str | None = None,
) -> str:
    record = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role=role, kind=kind, allowed_tools=("run_study",)),
        actor_id=_ACTOR,
        parent_sub_agent_id=parent_sub_agent_id,
    )
    return record.sub_agent_id


def _node_scoped(chat: ChatService, chat_id: str, node: str) -> list:
    messages = []
    for message in chat.list_messages(_WORKSPACE, chat_id):
        if message.data is not None and message.data.get("subAgentId") == node:
            messages.append(message)
    return messages


def test_delivery_to_a_node_persists_once_and_schedules_once() -> None:
    chat, chat_id, store, lifecycle = _setup()
    node = _spawn(lifecycle, chat_id)
    scheduled: list[tuple[str, str, str]] = []
    service = DeliveryService(
        chat,
        schedule=lambda workspace, chat, node_id: scheduled.append(
            (workspace, chat, node_id)
        ),
    )
    datum = datum_from_result(
        DatumType.SUB_AGENT_STATUS.value,
        ok=True,
        structured={"status": "completed"},
        text="node update",
    )

    first = service.deliver(Anchor(_WORKSPACE, chat_id, node), datum)
    second = service.deliver(Anchor(_WORKSPACE, chat_id, node), datum)

    assert first.duplicate is False
    assert first.scheduled is True
    assert second.duplicate is True
    assert second.scheduled is False
    assert second.message.message_id == first.message.message_id
    assert scheduled == [(_WORKSPACE, chat_id, node)]
    assert len(_node_scoped(chat, chat_id, node)) == 1


def test_delivery_without_schedule_persists_a_durable_question_only() -> None:
    chat, chat_id, store, lifecycle = _setup()
    node = _spawn(lifecycle, chat_id)
    scheduled: list[tuple[str, str, str]] = []
    service = DeliveryService(
        chat,
        schedule=lambda workspace, chat, node_id: scheduled.append(
            (workspace, chat, node_id)
        ),
    )
    datum = datum_from_result(
        DatumType.OPERATOR_QUESTION.value,
        ok=True,
        structured={"kind": "operator_question"},
        text="What do you think?",
    )

    receipt = service.deliver(Anchor(_WORKSPACE, chat_id, node), datum, schedule=False)

    # The question is durable, but no turn is dispatched: only its accepted
    # answer may schedule one.
    assert receipt.duplicate is False
    assert receipt.scheduled is False
    assert scheduled == []
    assert len(_node_scoped(chat, chat_id, node)) == 1


def test_delivery_to_the_root_anchor_uses_the_root_scheduler_id() -> None:
    chat, chat_id, store, lifecycle = _setup()
    scheduled: list[tuple[str, str, str]] = []
    service = DeliveryService(
        chat,
        schedule=lambda workspace, chat, node_id: scheduled.append(
            (workspace, chat, node_id)
        ),
    )

    receipt = service.deliver(
        Anchor(_WORKSPACE, chat_id),
        datum_from_result(DatumType.TOOL_RESULT.value, ok=True, text="done"),
    )

    assert scheduled == [(_WORKSPACE, chat_id, ROOT_ANCHOR_ID)]
    assert receipt.message.data is not None
    assert receipt.message.data["subAgentId"] is None
    assert receipt.message.data[DELIVERY_KEY_FIELD] == receipt.idempotency_key


def test_deliver_child_result_reaches_a_node_anchor() -> None:
    chat, chat_id, store, lifecycle = _setup()
    parent = _spawn(lifecycle, chat_id, role="lead")
    child = _spawn(
        lifecycle,
        chat_id,
        role="critique",
        kind=SubAgentKind.ONE_SHOT,
        parent_sub_agent_id=parent,
    )
    scheduled: list[str] = []
    service = DeliveryService(
        chat, schedule=lambda workspace, chat, node_id: scheduled.append(node_id)
    )

    terminal = lifecycle.fail(_WORKSPACE, child, result={"failure": "boom"})
    first = service.deliver_child_result(Anchor(_WORKSPACE, chat_id, parent), terminal)
    second = service.deliver_child_result(Anchor(_WORKSPACE, chat_id, parent), terminal)

    assert scheduled == [parent]
    assert second.duplicate is True
    assert first.message.data is not None
    assert first.message.data["status"] == "failed"


def test_node_turn_runner_persists_the_transcript() -> None:
    chat, chat_id, store, lifecycle = _setup()
    node = _spawn(lifecycle, chat_id)
    provider = _FakeProvider(
        [
            _completion(
                tool_calls=(ToolCall(id="call-1", name="probe", arguments="{}"),)
            ),
            _completion(content="node answer"),
        ]
    )
    registry = SessionToolRegistry(
        [
            RegisteredTool(
                _function_spec("probe"),
                lambda arguments: ToolExecution(
                    ok=True, structured={"ran": True}, text="ok"
                ),
            )
        ]
    )
    captured: list[ToolContext] = []
    runner = Conversations(
        chat=chat,
        store=store,
        provider=provider,
        registry_factory=lambda context: (captured.append(context), registry)[1],
    )
    input_message = chat.create_message(
        _WORKSPACE,
        chat_id,
        actor_id=_ACTOR,
        body="please continue",
        role=MessageRole.USER,
        kind=TranscriptItemKind.SUB_AGENT,
        data={"subAgentId": node, "kind": "operator_input"},
    )

    result = runner.run_node_turn(_WORKSPACE, chat_id, node)

    assert result.content == "node answer"
    transcript = runner.transcript(Anchor(_WORKSPACE, chat_id, sub_agent_id=node))
    assistant = [
        message
        for message in transcript
        if message.role is MessageRole.ASSISTANT
        and message.kind is TranscriptItemKind.SUB_AGENT
    ]
    tools = [
        message
        for message in transcript
        if message.role is MessageRole.TOOL
        and message.kind is TranscriptItemKind.SUB_AGENT
    ]
    assert [message.body for message in assistant] == [
        "Requested tool call(s): probe",
        "node answer",
    ]
    assert assistant[0].data is not None
    assert assistant[0].data["toolCalls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "probe", "arguments": "{}"},
        }
    ]
    assert len(tools) == 1
    assert tools[0].data is not None
    assert tools[0].data["toolCallId"] == "call-1"
    assert tools[0].data["datumType"] == "probe"
    assert captured[0].sub_agent_id == node
    assert captured[0].trigger_message_id == input_message.message_id


def test_node_turn_runner_completes_a_one_shot_child_and_delivers_the_result() -> None:
    chat, chat_id, store, lifecycle = _setup()
    caller = _spawn(lifecycle, chat_id, role="chat-agent")
    child = _spawn(
        lifecycle,
        chat_id,
        role="calculator",
        kind=SubAgentKind.ONE_SHOT,
        parent_sub_agent_id=caller,
    )
    runner = Conversations(
        chat=chat,
        store=store,
        provider=_FakeProvider([_completion(content="19")]),
        registry_factory=lambda context: SessionToolRegistry([]),
        lifecycle=lifecycle,
    )

    runner.run_node_turn(_WORKSPACE, chat_id, child)

    completed = lifecycle.get(_WORKSPACE, child)
    assert completed is not None
    assert completed.status is SubAgentStatus.COMPLETED
    assert completed.result == {"summary": "19"}
    delivered = [
        message
        for message in _node_scoped(chat, chat_id, caller)
        if (message.data or {}).get("kind") == "child_result"
    ]
    assert delivered


def test_operator_request_is_a_flag_at_the_asking_node_and_accepts_a_reply() -> None:
    chat, chat_id, store, lifecycle = _setup()
    asking = _spawn(lifecycle, chat_id, kind=SubAgentKind.ONE_SHOT)
    sibling = _spawn(lifecycle, chat_id, role="sibling", kind=SubAgentKind.ONE_SHOT)
    lifecycle.start(_WORKSPACE, asking)
    lifecycle.start(_WORKSPACE, sibling)
    scheduled: list[str] = []
    service = ControlToolService(
        conversations=Conversations(
            chat=chat,
            store=store,
            lifecycle=lifecycle,
            # The request path consults no operator policy: a registry that
            # would deny every answer still lets the node ask.
            policies=OperatorPolicyRegistry(
                default=NodeOperatorPolicy(allowed_actor_kinds=frozenset())
            ),
            spawn_thread=lambda target: scheduled.append("woke"),
        )
    )
    provider = _FakeProvider(
        [
            _completion(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name=TOOL_REQUEST_OPERATOR_INPUT,
                        arguments=(
                            '{"message": "Approve the model?", "reason": "review"}'
                        ),
                    ),
                )
            ),
            _completion(content="resumed after the reply"),
        ]
    )
    context = ToolContext(
        workspace_id=_WORKSPACE,
        chat_id=chat_id,
        conversation_id=asking,
        actor_id=_ACTOR,
        sub_agent_id=asking,
        allowed_tools=(TOOL_REQUEST_OPERATOR_INPUT,),
    )
    registry = SessionToolRegistry(control_registered_tools(service, context))
    runner = Conversations(
        chat=chat,
        store=store,
        provider=provider,
        registry_factory=lambda context: registry,
    )

    runner.run_node_turn(_WORKSPACE, chat_id, asking)

    waiting = lifecycle.get(_WORKSPACE, asking)
    assert waiting is not None
    assert waiting.status is SubAgentStatus.NEEDS_INPUT
    assert waiting.accepts_input is True
    untouched = lifecycle.get(_WORKSPACE, sibling)
    assert untouched is not None
    assert untouched.status is SubAgentStatus.RUNNING
    assert untouched.accepts_input is False
    # Asking is a flag and a request only: it schedules no turn anywhere.
    assert scheduled == []

    # The request renders at the asking node as its own tool turn carrying the
    # message and reason; no sibling anchor receives anything.
    request_turns = [
        message
        for message in _node_scoped(chat, chat_id, asking)
        if message.role is MessageRole.TOOL
    ]
    assert len(request_turns) == 1
    tool_result = request_turns[0].tool_result
    assert tool_result is not None
    assert tool_result["structured"] == {
        "subAgentId": asking,
        "question": "Approve the model?",
        "reason": "review",
    }
    assert request_turns[0].data is not None
    assert request_turns[0].data["datumType"] == DatumType.OPERATOR_QUESTION.value
    assert _node_scoped(chat, chat_id, sibling) == []

    # The reply path is the only resume, and a human answer needs no policy or
    # approval configuration: the default human-only registry admits it.
    answer_service = Conversations(
        chat=chat,
        store=store,
        lifecycle=lifecycle,
        spawn_thread=lambda target: scheduled.append("woke"),
    )
    receipt = answer_service.answer(
        _WORKSPACE,
        chat_id,
        asking,
        body="approved",
        actor=AnsweringActor(_ACTOR, OperatorActorKind.HUMAN_OPERATOR),
    )
    assert receipt.scheduled is True
    assert scheduled == ["woke"]
    resumed = lifecycle.get(_WORKSPACE, asking)
    assert resumed is not None
    assert resumed.status is SubAgentStatus.RUNNING

    runner.run_node_turn(_WORKSPACE, chat_id, asking)

    assistant = [
        message
        for message in _node_scoped(chat, chat_id, asking)
        if message.role is MessageRole.ASSISTANT
    ]
    assert [message.body for message in assistant] == [
        "Requested tool call(s): request_operator_input",
        "resumed after the reply",
    ]


def test_operator_answer_drives_the_phase_turn_to_request_phase_completion() -> None:
    chat, chat_id, store, lifecycle = _setup()
    node = _spawn(lifecycle, chat_id, role="workflow-node", kind=SubAgentKind.ONE_SHOT)
    phase = _spawn(
        lifecycle, chat_id, kind=SubAgentKind.ONE_SHOT, parent_sub_agent_id=node
    )
    lifecycle.start(_WORKSPACE, phase)
    service = ControlToolService(
        conversations=Conversations(chat=chat, store=store, lifecycle=lifecycle)
    )

    def registry_factory(context: ToolContext) -> SessionToolRegistry:
        return SessionToolRegistry(control_registered_tools(service, context))

    asking = _FakeProvider(
        [
            _completion(
                tool_calls=(
                    ToolCall(
                        id="ask-1",
                        name=TOOL_REQUEST_OPERATOR_INPUT,
                        arguments='{"message": "Approve the contract?"}',
                    ),
                )
            ),
        ]
    )
    Conversations(
        chat=chat,
        store=store,
        provider=asking,
        registry_factory=registry_factory,
    ).run_node_turn(_WORKSPACE, chat_id, phase)
    waiting = lifecycle.get(_WORKSPACE, phase)
    assert waiting is not None
    assert waiting.status is SubAgentStatus.NEEDS_INPUT
    assert waiting.accepts_input is True

    completing = _FakeProvider(
        [
            _completion(
                tool_calls=(
                    ToolCall(
                        id="done-1",
                        name="request_phase_completion",
                        arguments='{"completion": {"advance": true}}',
                    ),
                )
            ),
        ]
    )
    runner = Conversations(
        chat=chat,
        store=store,
        provider=completing,
        registry_factory=registry_factory,
    )
    driven: list[str] = []

    def wake(run: Callable[[], None]) -> None:
        driven.append(phase)
        runner.run_node_turn(_WORKSPACE, chat_id, phase)

    answering = Conversations(
        chat=chat,
        store=store,
        lifecycle=lifecycle,
        spawn_thread=wake,
    )

    receipt = answering.answer(
        _WORKSPACE,
        chat_id,
        phase,
        body="approved",
        actor=AnsweringActor(_ACTOR, OperatorActorKind.HUMAN_OPERATOR),
    )

    assert receipt.scheduled is True
    assert driven == [phase]
    completed = lifecycle.get(_WORKSPACE, phase)
    assert completed is not None
    assert completed.status is SubAgentStatus.COMPLETED
    assert completed.result == {"advance": True, "intentEpoch": 0}
    deliveries = [
        message
        for message in _node_scoped(chat, chat_id, node)
        if message.data is not None and message.data.get("kind") == "child_result"
    ]
    assert len(deliveries) == 1
    assert deliveries[0].data["childSubAgentId"] == phase


def test_re_delivered_result_does_not_duplicate_a_node_turn() -> None:
    chat, chat_id, store, lifecycle = _setup()
    node = _spawn(lifecycle, chat_id, role="lead")
    child = _spawn(
        lifecycle,
        chat_id,
        role="critique",
        kind=SubAgentKind.ONE_SHOT,
        parent_sub_agent_id=node,
    )
    provider = _FakeProvider([_completion(content="handled the result")])
    runner = Conversations(
        chat=chat,
        store=store,
        provider=provider,
        registry_factory=lambda context: SessionToolRegistry(),
    )
    turns: list[str] = []

    def schedule(workspace_id: str, chat: str, node_id: str) -> None:
        turns.append(node_id)
        runner.run_node_turn(workspace_id, chat, node_id)

    service = DeliveryService(chat, schedule=schedule)
    terminal = lifecycle.complete(_WORKSPACE, child, result={"finding": "none"})
    anchor = Anchor(_WORKSPACE, chat_id, node)

    service.deliver_child_result(anchor, terminal)
    service.deliver_child_result(anchor, terminal)

    assistant = [
        message
        for message in _node_scoped(chat, chat_id, node)
        if message.role is MessageRole.ASSISTANT
    ]
    assert turns == [node]
    assert [message.body for message in assistant] == ["handled the result"]


# -- datum-less deliveries (REQ-A1/A4 prerequisite) --------------------------


def test_datum_less_delivery_requires_an_explicit_key_and_body() -> None:
    chat, chat_id, store, lifecycle = _setup()
    node = _spawn(lifecycle, chat_id)
    service = DeliveryService(chat)

    with pytest.raises(DeliveryError):
        service.deliver(Anchor(_WORKSPACE, chat_id, node), None, body="hello")
    with pytest.raises(DeliveryError):
        service.deliver(
            Anchor(_WORKSPACE, chat_id, node),
            None,
            idempotency_key="answer-1",
        )


def test_datum_less_delivery_persists_without_a_datum_payload_and_dedupes() -> None:
    chat, chat_id, store, lifecycle = _setup()
    node = _spawn(lifecycle, chat_id)
    scheduled: list[str] = []
    service = DeliveryService(
        chat, schedule=lambda workspace, chat, node_id: scheduled.append(node_id)
    )

    first = service.deliver(
        Anchor(_WORKSPACE, chat_id, node),
        None,
        body="the operator's answer",
        role=MessageRole.USER,
        kind=TranscriptItemKind.SUB_AGENT,
        data={"kind": "operator_input"},
        idempotency_key="answer-1",
    )
    second = service.deliver(
        Anchor(_WORKSPACE, chat_id, node),
        None,
        body="the operator's answer",
        role=MessageRole.USER,
        kind=TranscriptItemKind.SUB_AGENT,
        data={"kind": "operator_input"},
        idempotency_key="answer-1",
    )

    assert first.duplicate is False
    assert first.scheduled is True
    assert scheduled == [node]
    message = first.message
    assert message.role is MessageRole.USER
    assert message.kind is TranscriptItemKind.SUB_AGENT
    assert message.body == "the operator's answer"
    assert message.tool_result is None
    assert message.data is not None
    assert message.data == {
        "kind": "operator_input",
        "subAgentId": node,
        DELIVERY_KEY_FIELD: "answer-1",
    }
    # The redelivery resolves to the durable message and wakes nothing.
    assert second.duplicate is True
    assert second.scheduled is False
    assert second.message.message_id == first.message.message_id
    assert scheduled == [node]
    assert len(_node_scoped(chat, chat_id, node)) == 1


# -- Conversations.answer: the admission layer (REQ-A4) ----------------------


def test_answer_denies_an_agent_actor_and_persists_nothing() -> None:
    chat, chat_id, store, lifecycle = _setup()
    node = _spawn(lifecycle, chat_id)
    woke: list[object] = []
    conversations = Conversations(
        chat=chat, store=store, lifecycle=lifecycle, spawn_thread=woke.append
    )

    with pytest.raises(OperatorPolicyDenied) as error:
        conversations.answer(
            _WORKSPACE,
            chat_id,
            node,
            body="an agent answer",
            actor=AnsweringActor("other-agent", OperatorActorKind.AGENT),
            idempotency_key="answer-1",
        )

    provenance = error.value.as_dict()
    assert provenance["code"] == "operator_policy_denied"
    assert provenance["actorKind"] == "agent"
    assert _node_scoped(chat, chat_id, node) == []
    assert woke == []


def test_answer_fails_closed_for_a_one_shot_or_unknown_node() -> None:
    chat, chat_id, store, lifecycle = _setup()
    node = _spawn(lifecycle, chat_id, kind=SubAgentKind.ONE_SHOT)
    conversations = Conversations(
        chat=chat, store=store, lifecycle=lifecycle, spawn_thread=lambda target: None
    )

    with pytest.raises(NodeInputNotAccepted):
        conversations.answer(
            _WORKSPACE,
            chat_id,
            node,
            body="hello",
            actor=AnsweringActor(_ACTOR),
        )
    with pytest.raises(NodeInputNotFound):
        conversations.answer(
            _WORKSPACE,
            chat_id,
            "missing",
            body="hello",
            actor=AnsweringActor(_ACTOR),
        )
    assert _node_scoped(chat, chat_id, node) == []


def test_answer_resumes_wakes_and_persists_the_provenance_payload() -> None:
    chat, chat_id, store, lifecycle = _setup()
    parent = _spawn(lifecycle, chat_id, role="lead")
    node = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="phase", kind=SubAgentKind.ONE_SHOT, allowed_tools=()),
        actor_id=_ACTOR,
        parent_sub_agent_id=parent,
        workflow_execution_id="exec-1",
    ).sub_agent_id
    chat.persistence.save(
        WorkflowExecution(
            workspace_id=_WORKSPACE,
            execution_id="exec-1",
            chat_id=chat_id,
            trigger_message_id="msg-1",
            workflow_id="hypothesis-development",
            workflow_version="1.0.0",
            requested_by=_ACTOR,
            created_at=datetime.now(UTC),
            created_by=_ACTOR,
        )
    )
    lifecycle.request_input(_WORKSPACE, node)
    woke: list[object] = []
    conversations = Conversations(
        chat=chat, store=store, lifecycle=lifecycle, spawn_thread=woke.append
    )

    receipt = conversations.answer(
        _WORKSPACE,
        chat_id,
        node,
        body="approved",
        actor=AnsweringActor(_ACTOR, OperatorActorKind.HUMAN_OPERATOR),
        idempotency_key="answer-1",
    )

    assert receipt.decision.allowed is True
    assert receipt.duplicate is False
    resumed = lifecycle.get(_WORKSPACE, node)
    assert resumed is not None
    assert resumed.status is SubAgentStatus.RUNNING
    assert resumed.accepts_input is False
    message = receipt.message
    assert message.actor_id == _ACTOR
    assert message.role is MessageRole.USER
    assert message.kind is TranscriptItemKind.SUB_AGENT
    assert message.body == "approved"
    assert message.execution_id == "exec-1"
    data = message.data
    assert data is not None
    assert data["subAgentId"] == node
    assert data["parentSubAgentId"] == parent
    assert data["kind"] == "operator_input"
    assert data["actorKind"] == "human_operator"
    policy = data["operatorPolicy"]
    assert isinstance(policy, dict) and policy["allowed"] is True
    assert data[DELIVERY_KEY_FIELD] == "answer-1"
    assert woke != []


def test_answer_redelivery_is_idempotent_on_an_active_conversation_node() -> None:
    chat, chat_id, store, lifecycle = _setup()
    parent = _spawn(lifecycle, chat_id, role="lead")
    node = _spawn(lifecycle, chat_id, parent_sub_agent_id=parent)
    lifecycle.request_input(_WORKSPACE, node)
    woke: list[object] = []
    conversations = Conversations(
        chat=chat, store=store, lifecycle=lifecycle, spawn_thread=woke.append
    )

    first = conversations.answer(
        _WORKSPACE,
        chat_id,
        node,
        body="the answer",
        actor=AnsweringActor(_ACTOR, OperatorActorKind.HUMAN_OPERATOR),
        idempotency_key="answer-1",
    )
    resumed = lifecycle.get(_WORKSPACE, node)
    assert resumed is not None
    assert resumed.status is SubAgentStatus.RUNNING

    retry = conversations.answer(
        _WORKSPACE,
        chat_id,
        node,
        body="the answer",
        actor=AnsweringActor(_ACTOR, OperatorActorKind.HUMAN_OPERATOR),
        idempotency_key="answer-1",
    )

    assert first.duplicate is False
    assert retry.duplicate is True
    assert retry.message.message_id == first.message.message_id
    assert len(_node_scoped(chat, chat_id, node)) == 1
    # The redelivery wakes nothing: one answer, one scheduled turn.
    assert len(woke) == 1
