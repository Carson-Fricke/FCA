"""Control-tool command behavior."""

from __future__ import annotations

import pytest

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.control import (
    TOOL_REQUEST_PHASE_COMPLETION,
    TOOL_SPAWN_SUB_AGENT,
    AwaitResult,
    ControlToolService,
    PhaseCompletionRejected,
)
from thesis.agent.runtime.control_tools import (
    CONTROL_NODE_TOOL_NAMES,
    control_registered_tools,
)
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import AnsweringActor, OperatorActorKind
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat.contracts import (
    AgentInvocation,
    SubAgentKind,
    SubAgentStatus,
    WorkflowDefinition,
)
from thesis.chat.service import ChatService, InMemoryChatPersistence

_WORKSPACE = "ws-test"
_ACTOR = "operator-1"


def _service(
    **conversations_fields,
) -> tuple[ControlToolService, SubAgentLifecycle, ChatService]:
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    conversations = Conversations(
        chat=chat, store=store, lifecycle=lifecycle, **conversations_fields
    )
    service = ControlToolService(conversations=conversations)
    return service, lifecycle, chat


def test_spawn_records_a_first_class_tool_invocation() -> None:
    service, lifecycle, chat = _service()
    chat_id = chat.list_chats(_WORKSPACE)[0].chat_id
    result = service.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
        conversation_id="subagent-root",
        prompt="do the work",
        idempotency_key="key-1",
    )
    assert result.sub_agent.parent_sub_agent_id is None
    saved = chat.persistence.get(
        AgentInvocation, _WORKSPACE, result.invocation.invocation_id
    )
    assert saved is not None
    assert saved.tool_name == TOOL_SPAWN_SUB_AGENT
    assert saved.idempotency_key == "key-1"
    assert result.sub_agent.spawned_by_invocation_id == saved.invocation_id


def test_await_returns_non_terminal_then_terminal() -> None:
    service, lifecycle, chat = _service()
    chat_id = chat.list_chats(_WORKSPACE)[0].chat_id
    child = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    lifecycle.start(_WORKSPACE, child.sub_agent_id)
    running: AwaitResult = service.await_sub_agent(_WORKSPACE, child.sub_agent_id)
    assert not running.terminal
    service.complete_sub_agent(
        _WORKSPACE, child.sub_agent_id, result={"verdict": "supported"}
    )
    done = service.await_sub_agent(_WORKSPACE, child.sub_agent_id)
    assert done.terminal
    assert done.result == {"verdict": "supported"}


def test_stale_epoch_completion_is_rejected_in_turn() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat_id = chat.create_chat(
        _WORKSPACE, title="Test chat", owner_actor_id=_ACTOR
    ).chat_id
    execution = chat.launch_workflow(
        _WORKSPACE,
        chat_id,
        workflow=WorkflowDefinition(
            workflow_id="hypothesis-development",
            version="1.0.0",
            title="Hypothesis development",
            phases=("hypothesis_creation",),
        ),
        actor_id=_ACTOR,
        body="Test the premise.",
    )
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    node = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="workflow-node", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    phase = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(
            role="falsification", kind=SubAgentKind.ONE_SHOT, phase="falsification"
        ),
        actor_id=_ACTOR,
        parent_sub_agent_id=node.sub_agent_id,
        workflow_execution_id=execution.execution_id,
    )
    service = ControlToolService(
        conversations=Conversations(chat=chat, store=store, lifecycle=lifecycle),
        execution_epochs=lambda _workspace, _execution: 2,
    )

    def completion_tool(intent_epoch: int):
        context = ToolContext(
            workspace_id=_WORKSPACE,
            chat_id=chat_id,
            conversation_id=phase.sub_agent_id,
            actor_id=_ACTOR,
            sub_agent_id=phase.sub_agent_id,
            parent_sub_agent_id=node.sub_agent_id,
            workflow_execution_id=execution.execution_id,
            allowed_tools=CONTROL_NODE_TOOL_NAMES,
            intent_epoch=intent_epoch,
        )
        return {tool.name: tool for tool in control_registered_tools(service, context)}[
            TOOL_REQUEST_PHASE_COMPLETION
        ]

    rejected = completion_tool(1).handler({"completion": {"advance": True}})

    assert rejected.ok is False
    assert rejected.terminal is False
    assert rejected.failure is not None
    assert "intent epoch" in str(rejected.failure["message"])
    still_open = lifecycle.get(_WORKSPACE, phase.sub_agent_id)
    assert still_open is not None
    assert still_open.status is SubAgentStatus.PENDING
    assert not any(
        message.data is not None
        and message.data.get("childSubAgentId") == phase.sub_agent_id
        for message in chat.list_messages(_WORKSPACE, chat_id)
    )

    accepted = completion_tool(2).handler({"completion": {"advance": True}})

    assert accepted.ok is True
    completed = lifecycle.get(_WORKSPACE, phase.sub_agent_id)
    assert completed is not None
    assert completed.status is SubAgentStatus.COMPLETED
    assert completed.result == {"advance": True, "intentEpoch": 2}


def test_request_operator_input_pauses_the_node() -> None:
    service, lifecycle, chat = _service()
    chat_id = chat.list_chats(_WORKSPACE)[0].chat_id
    child = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    lifecycle.start(_WORKSPACE, child.sub_agent_id)
    request = service.request_operator_input(
        _WORKSPACE, child.sub_agent_id, message="Confirm the model?", reason="review"
    )
    assert request.message == "Confirm the model?"
    record = lifecycle.get(_WORKSPACE, child.sub_agent_id)
    assert record is not None
    assert record.status is SubAgentStatus.NEEDS_INPUT
    assert record.accepts_input
    with pytest.raises(ValueError):
        service.request_operator_input(_WORKSPACE, child.sub_agent_id, message="   ")


def test_operator_input_pause_and_answer_share_the_input_path() -> None:
    service, lifecycle, chat = _service()
    chat_id = chat.list_chats(_WORKSPACE)[0].chat_id
    store = PersistenceSubAgentStore(chat.persistence)
    child = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    lifecycle.start(_WORKSPACE, child.sub_agent_id)

    service.request_operator_input(
        _WORKSPACE, child.sub_agent_id, message="Approve the model?"
    )

    record = lifecycle.get(_WORKSPACE, child.sub_agent_id)
    assert record is not None
    assert record.status is SubAgentStatus.NEEDS_INPUT
    assert record.accepts_input is True

    woke: list[object] = []
    conversations = Conversations(
        chat=chat, store=store, lifecycle=lifecycle, spawn_thread=woke.append
    )
    receipt = conversations.answer(
        _WORKSPACE,
        chat_id,
        child.sub_agent_id,
        body="approved",
        actor=AnsweringActor(actor_id=_ACTOR, kind=OperatorActorKind.HUMAN_OPERATOR),
    )

    assert receipt.scheduled is True
    resumed = lifecycle.get(_WORKSPACE, child.sub_agent_id)
    assert resumed is not None
    assert resumed.status is SubAgentStatus.RUNNING
    answers = [
        message
        for message in chat.list_messages(_WORKSPACE, chat_id)
        if message.data is not None
        and message.data.get("subAgentId") == child.sub_agent_id
    ]
    assert [message.body for message in answers] == ["approved"]
    assert answers[0].data is not None
    assert answers[0].data["kind"] == "operator_input"


def test_contract_validator_rejection_precedes_application() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat_id = chat.create_chat(
        _WORKSPACE, title="validation chat", owner_actor_id=_ACTOR
    ).chat_id
    execution = chat.launch_workflow(
        _WORKSPACE,
        chat_id,
        workflow=WorkflowDefinition(
            workflow_id="hypothesis-development",
            version="1.0.0",
            title="Hypothesis development",
            phases=("hypothesis_creation",),
        ),
        actor_id=_ACTOR,
        body="Test the premise.",
    )
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    node = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="workflow-node", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    phase = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(
            role="hypothesis-creation",
            kind=SubAgentKind.ONE_SHOT,
            phase="hypothesis_creation",
        ),
        actor_id=_ACTOR,
        parent_sub_agent_id=node.sub_agent_id,
        workflow_execution_id=execution.execution_id,
    )
    applied: list[object] = []
    rejection: list[str | None] = [
        "the authored study program does not compile in the harness"
    ]
    service = ControlToolService(
        conversations=Conversations(chat=chat, store=store, lifecycle=lifecycle),
        phase_completions=lambda _workspace, _record, completion: (
            applied.append(completion) or {"applied": True}
        ),
        contract_validator=lambda _workspace, _record, _completion: rejection[0],
    )

    with pytest.raises(PhaseCompletionRejected) as error:
        service.complete_phase(
            _WORKSPACE, phase.sub_agent_id, completion={"hypothesis": {}}
        )

    assert "does not compile" in str(error.value)
    assert applied == []
    still_open = lifecycle.get(_WORKSPACE, phase.sub_agent_id)
    assert still_open is not None
    assert still_open.status is SubAgentStatus.PENDING

    rejection[0] = None
    service.complete_phase(
        _WORKSPACE, phase.sub_agent_id, completion={"hypothesis": {}}
    )
    assert applied == [{"hypothesis": {}}]
    completed = lifecycle.get(_WORKSPACE, phase.sub_agent_id)
    assert completed is not None
    assert completed.status is SubAgentStatus.COMPLETED


def test_await_sub_agent_waits_for_a_child_to_finish() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat_id = chat.create_chat(
        _WORKSPACE, title="await chat", owner_actor_id=_ACTOR
    ).chat_id
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    child = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="calculator", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    lifecycle.start(_WORKSPACE, child.sub_agent_id)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        lifecycle.complete(_WORKSPACE, child.sub_agent_id, result={"summary": "24"})

    service = ControlToolService(
        conversations=Conversations(
            chat=chat,
            store=store,
            lifecycle=lifecycle,
            await_sleep=sleep,
            await_monotonic=lambda: 0.0,
            await_timeout_seconds=5.0,
            poll_interval_seconds=0.01,
        )
    )

    result = service.await_sub_agent(_WORKSPACE, child.sub_agent_id)

    assert sleeps == [0.01]
    assert result.terminal is True
    assert result.result == {"summary": "24"}


def test_await_sub_agent_returns_the_current_state_at_the_timeout() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat_id = chat.create_chat(
        _WORKSPACE, title="await chat", owner_actor_id=_ACTOR
    ).chat_id
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    child = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="calculator", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    lifecycle.start(_WORKSPACE, child.sub_agent_id)
    ticks = iter([0.0, 10.0, 10.0])
    sleeps: list[float] = []
    service = ControlToolService(
        conversations=Conversations(
            chat=chat,
            store=store,
            lifecycle=lifecycle,
            await_sleep=lambda seconds: sleeps.append(seconds),
            await_monotonic=lambda: next(ticks),
            await_timeout_seconds=5.0,
        )
    )

    result = service.await_sub_agent(_WORKSPACE, child.sub_agent_id)

    assert sleeps == []
    assert result.terminal is False
    assert result.status == "running"
