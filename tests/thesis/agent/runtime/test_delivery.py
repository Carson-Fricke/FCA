"""Unified delivery, node turns, and per-node operator policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from thesis.agent.runtime.child_runner import NodeTurnRunner
from thesis.agent.runtime.datum import DatumType, datum_from_result
from thesis.agent.runtime.delivery import (
    DELIVERY_KEY_FIELD,
    ROOT_ANCHOR_ID,
    Anchor,
    DeliveryService,
    NodeInputNotAccepted,
    NodeInputNotFound,
    NodeInputService,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.notify import SubAgentNotifier
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
    TranscriptItemKind,
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


def test_notifier_delivers_child_results_through_the_single_path() -> None:
    chat, chat_id, store, lifecycle = _setup()
    parent = _spawn(lifecycle, chat_id, role="lead")
    child = _spawn(
        lifecycle,
        chat_id,
        role="critique",
        kind=SubAgentKind.ONE_SHOT,
        parent_sub_agent_id=parent,
    )
    scheduled: list[tuple[str, str, str]] = []
    notifier = SubAgentNotifier(
        chat,
        schedule=lambda workspace, chat, node_id: scheduled.append(
            (workspace, chat, node_id)
        ),
    )
    lifecycle.start(_WORKSPACE, child)
    terminal = lifecycle.complete(
        _WORKSPACE, child, result={"finding": "no regularity"}
    )

    message = notifier.notify(terminal)
    duplicate = notifier.notify(terminal)

    assert message is not None
    assert duplicate is not None
    assert duplicate.message_id == message.message_id
    assert message.data is not None
    assert message.data["subAgentId"] == parent
    assert message.data["childSubAgentId"] == child
    assert message.data["kind"] == "child_result"
    assert scheduled == [(_WORKSPACE, chat_id, parent)]


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


def test_human_only_node_rejects_an_agent_answer() -> None:
    chat, chat_id, store, lifecycle = _setup()
    node = _spawn(lifecycle, chat_id)
    scheduled: list[str] = []
    service = NodeInputService(
        chat=chat,
        store=store,
        schedule=lambda workspace, chat, node_id: scheduled.append(node_id),
    )

    with pytest.raises(OperatorPolicyDenied) as error:
        service.accept(
            _WORKSPACE,
            chat_id,
            node,
            body="an agent answer",
            actor=AnsweringActor("other-agent", OperatorActorKind.AGENT),
        )

    provenance = error.value.as_dict()
    assert provenance["code"] == "operator_policy_denied"
    assert provenance["actorKind"] == "agent"
    assert provenance["policy"] == "human_only"
    assert _node_scoped(chat, chat_id, node) == []
    assert scheduled == []


def test_authorized_parent_answer_is_accepted_with_provenance() -> None:
    chat, chat_id, store, lifecycle = _setup()
    parent = _spawn(lifecycle, chat_id, role="lead")
    child = _spawn(lifecycle, chat_id, role="critique", parent_sub_agent_id=parent)
    policies = OperatorPolicyRegistry(
        role_policies={
            "critique": NodeOperatorPolicy(
                allowed_actor_kinds=frozenset(
                    {
                        OperatorActorKind.HUMAN_OPERATOR,
                        OperatorActorKind.PARENT_AGENT,
                    }
                )
            )
        }
    )
    scheduled: list[str] = []
    service = NodeInputService(
        chat=chat,
        store=store,
        policies=policies,
        schedule=lambda workspace, chat, node_id: scheduled.append(node_id),
    )

    receipt = service.accept(
        _WORKSPACE,
        chat_id,
        child,
        body="the parent's answer",
        actor=AnsweringActor(parent, OperatorActorKind.PARENT_AGENT),
        idempotency_key="answer-1",
    )

    assert receipt.decision.allowed is True
    assert receipt.message.actor_id == parent
    data = receipt.message.data
    assert data is not None
    assert data["subAgentId"] == child
    assert data["kind"] == "operator_input"
    assert data["actorKind"] == "parent_agent"
    assert data["operatorPolicy"]["allowedActorKinds"] == [
        "human_operator",
        "parent_agent",
    ]
    assert scheduled == [child]

    retry = service.accept(
        _WORKSPACE,
        chat_id,
        child,
        body="the parent's answer",
        actor=AnsweringActor(parent, OperatorActorKind.PARENT_AGENT),
        idempotency_key="answer-1",
    )

    assert retry.duplicate is True
    assert retry.message.message_id == receipt.message.message_id
    assert scheduled == [child]


def test_human_operator_answer_is_accepted_by_default() -> None:
    chat, chat_id, store, lifecycle = _setup()
    node = _spawn(lifecycle, chat_id)
    scheduled: list[str] = []
    service = NodeInputService(
        chat=chat,
        store=store,
        schedule=lambda workspace, chat, node_id: scheduled.append(node_id),
    )

    receipt = service.accept(
        _WORKSPACE,
        chat_id,
        node,
        body="operator answer",
        actor=AnsweringActor("acct-9", OperatorActorKind.HUMAN_OPERATOR),
    )

    assert receipt.decision.allowed is True
    assert receipt.message.actor_id == "acct-9"
    assert scheduled == [node]


def test_node_input_fails_closed_for_a_one_shot_or_unknown_node() -> None:
    chat, chat_id, store, lifecycle = _setup()
    node = _spawn(lifecycle, chat_id, kind=SubAgentKind.ONE_SHOT)
    service = NodeInputService(chat=chat, store=store)

    with pytest.raises(NodeInputNotAccepted):
        service.accept(
            _WORKSPACE,
            chat_id,
            node,
            body="hello",
            actor=AnsweringActor("acct-9"),
        )
    with pytest.raises(NodeInputNotFound):
        service.accept(
            _WORKSPACE,
            chat_id,
            "missing",
            body="hello",
            actor=AnsweringActor("acct-9"),
        )


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
    captured: list[object] = []
    runner = NodeTurnRunner(
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

    result = runner.run(_WORKSPACE, chat_id, node)

    assert result.content == "node answer"
    transcript = runner.transcript(_WORKSPACE, chat_id, node)
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
    assert [message.body for message in assistant] == ["node answer"]
    assert len(tools) == 1
    assert tools[0].data is not None
    assert tools[0].data["toolCallId"] == "call-1"
    assert tools[0].data["datumType"] == "probe"
    assert captured[0].sub_agent_id == node
    assert captured[0].trigger_message_id == input_message.message_id


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
    runner = NodeTurnRunner(
        chat=chat,
        store=store,
        provider=provider,
        registry_factory=lambda context: SessionToolRegistry(),
    )
    turns: list[str] = []

    def schedule(workspace_id: str, chat: str, node_id: str) -> None:
        turns.append(node_id)
        runner.run(workspace_id, chat, node_id)

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
