"""Tests for the structured hypothesis-development chat trigger."""

from __future__ import annotations

import json

import pytest

from thesis.chat import (
    CHAT_MESSAGE,
    PROVIDER_DELTA,
    ChatReplyService,
    ChatService,
    InMemoryChatStreamBroker,
)
from thesis.chat.contracts import MessageRole, TranscriptItemKind
from thesis.execution.events import ExecutionEventType
from thesis.workflows.hypothesis_development import (
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    VERDICT_SUPPORTED,
    FalsificationVerdict,
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentPhase,
    HypothesisDevelopmentRequest,
)

WORKSPACE = "workspace-a"
IDEAS = "Momentum in SPY keeps going because the market underreacts."


class FakeWorkflow:
    """Minimal launchable development workflow backed by a real ChatService."""

    def __init__(self, chat: ChatService) -> None:
        self._chat = chat

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        ideas: str,
        investigation_id: str | None = None,
        trigger_message_id: str | None = None,
    ) -> HypothesisDevelopmentRequest:
        if trigger_message_id is None:
            execution = self._chat.launch_workflow(
                workspace_id,
                chat_id,
                workflow=HYPOTHESIS_DEVELOPMENT_WORKFLOW,
                actor_id=actor_id,
                body=ideas,
                investigation_id=investigation_id,
            )
        else:
            execution = self._chat.launch_workflow(
                workspace_id,
                chat_id,
                workflow=HYPOTHESIS_DEVELOPMENT_WORKFLOW,
                actor_id=actor_id,
                message_id=trigger_message_id,
                investigation_id=investigation_id,
            )
        self._chat.create_message(
            workspace_id,
            chat_id,
            actor_id=actor_id,
            body=HYPOTHESIS_DEVELOPMENT_WORKFLOW.title,
            role=MessageRole.SYSTEM,
            kind=TranscriptItemKind.WORKFLOW,
            data={
                "currentPhase": execution.phase,
                "executionId": execution.execution_id,
                "status": execution.state.value,
                "title": HYPOTHESIS_DEVELOPMENT_WORKFLOW.title,
                "version": execution.workflow_version,
                "workflowId": execution.workflow_id,
            },
            execution_id=execution.execution_id,
        )
        return HypothesisDevelopmentRequest(
            workspace_id=workspace_id,
            chat_id=chat_id,
            execution_id=execution.execution_id,
            trigger_message_id=execution.trigger_message_id,
            requested_by=actor_id,
            ideas=ideas,
            intent_epoch=execution.intent_epoch,
            created_at=execution.created_at,
        )


class FakeDevelopmentRuntime:
    """Deterministic runtime returning a canned completed outcome."""

    def __init__(self) -> None:
        self.requests: list[HypothesisDevelopmentRequest] = []

    def build_hypothesis_development(self, *, chat: ChatService) -> FakeWorkflow:
        return FakeWorkflow(chat)

    def execute_hypothesis_development(
        self,
        request: HypothesisDevelopmentRequest,
        *,
        chat: ChatService,
    ) -> HypothesisDevelopmentOutcome:
        del chat
        self.requests.append(request)
        return HypothesisDevelopmentOutcome(
            workspace_id=request.workspace_id,
            execution_id=request.execution_id,
            phase=HypothesisDevelopmentPhase.COMPLETED,
            fenced=False,
            transitions=(),
            verdict=FalsificationVerdict(
                VERDICT_SUPPORTED, "the hypothesis survived falsification"
            ),
        )


def _trigger(ideas: str = IDEAS) -> str:
    return json.dumps({"workflow": "hypothesis-development", "ideas": ideas})


def test_chat_message_launches_hypothesis_development() -> None:
    chat = ChatService()
    broker = InMemoryChatStreamBroker()
    runtime = FakeDevelopmentRuntime()
    reply = ChatReplyService(
        chat_service=chat,
        broker=broker,
        provider=lambda history: iter(()),
        development_runtime=runtime,
    )
    chat_id = chat.create_chat(
        WORKSPACE, title="Research", owner_actor_id="operator"
    ).chat_id
    chat.create_message(WORKSPACE, chat_id, actor_id="operator", body=_trigger())

    message = reply.respond(WORKSPACE, chat_id)

    assert message is not None
    assert message.role is MessageRole.ASSISTANT
    assert "supported" in message.body
    assert len(runtime.requests) == 1
    executions = chat.list_executions(WORKSPACE)
    assert len(executions) == 1
    assert executions[0].workflow_id == "hypothesis-development"
    replay = chat.replay_events(WORKSPACE, executions[0].execution_id)
    assert ExecutionEventType.WORKFLOW_LAUNCHED in [
        event.event_type for event in replay.events
    ]
    frames = broker.read(WORKSPACE, chat_id)
    assert frames[0].event == PROVIDER_DELTA
    assert frames[-2].event == CHAT_MESSAGE
    assert frames[-1].data["messageIdFinal"] == message.message_id


@pytest.mark.parametrize("envelope", [False, True])
def test_trigger_binds_to_the_operator_message(envelope: bool) -> None:
    chat = ChatService()
    broker = InMemoryChatStreamBroker()
    runtime = FakeDevelopmentRuntime()
    reply = ChatReplyService(
        chat_service=chat,
        broker=broker,
        provider=lambda history: iter(()),
        development_runtime=runtime,
    )
    chat_id = chat.create_chat(
        WORKSPACE, title="Research", owner_actor_id="operator"
    ).chat_id
    body = _trigger() if envelope else PLAIN_IDEAS
    operator = chat.create_message(WORKSPACE, chat_id, actor_id="operator", body=body)

    reply.respond(WORKSPACE, chat_id)

    messages = chat.list_messages(WORKSPACE, chat_id)
    user_messages = [item for item in messages if item.role is MessageRole.USER]
    assert len(user_messages) == 1
    assert user_messages[0].message_id == operator.message_id
    assert user_messages[0].body == body
    anchors = [item for item in messages if item.kind is TranscriptItemKind.WORKFLOW]
    assert len(anchors) == 1
    assert len(runtime.requests) == 1
    assert runtime.requests[0].trigger_message_id == operator.message_id


def test_development_trigger_fails_closed_without_a_runtime() -> None:
    chat = ChatService()
    broker = InMemoryChatStreamBroker()
    reply = ChatReplyService(
        chat_service=chat,
        broker=broker,
        provider=lambda history: iter(()),
    )
    chat_id = chat.create_chat(
        WORKSPACE, title="Research", owner_actor_id="operator"
    ).chat_id
    chat.create_message(WORKSPACE, chat_id, actor_id="operator", body=_trigger())

    assert reply.respond(WORKSPACE, chat_id) is None
    frames = broker.read(WORKSPACE, chat_id)
    assert any(
        frame.data.get("error") == "workflow runtime is not configured"
        for frame in frames
    )
    assert chat.list_executions(WORKSPACE) == ()


@pytest.mark.parametrize(
    "body",
    [
        "just a normal message",
        json.dumps({"workflow": "hypothesis-development"}),
        json.dumps({"workflow": "hypothesis-development", "ideas": "   "}),
    ],
)
def test_non_development_messages_use_the_provider(body: str) -> None:
    chat = ChatService()
    broker = InMemoryChatStreamBroker()
    runtime = FakeDevelopmentRuntime()
    reply = ChatReplyService(
        chat_service=chat,
        broker=broker,
        provider=lambda history: iter(("plain reply",)),
        development_runtime=runtime,
    )
    chat_id = chat.create_chat(
        WORKSPACE, title="Research", owner_actor_id="operator"
    ).chat_id
    chat.create_message(WORKSPACE, chat_id, actor_id="operator", body=body)

    message = reply.respond(WORKSPACE, chat_id)

    assert message is not None
    assert message.body == "plain reply"
    assert runtime.requests == []


PLAIN_IDEAS = "Momentum in SPY keeps going because the market underreacts."


def test_plain_language_idea_launches_hypothesis_development() -> None:
    chat = ChatService()
    broker = InMemoryChatStreamBroker()
    runtime = FakeDevelopmentRuntime()
    reply = ChatReplyService(
        chat_service=chat,
        broker=broker,
        provider=lambda history: iter(("plain reply",)),
        development_runtime=runtime,
    )
    chat_id = chat.create_chat(
        WORKSPACE, title="Research", owner_actor_id="operator"
    ).chat_id
    chat.create_message(WORKSPACE, chat_id, actor_id="operator", body=PLAIN_IDEAS)

    message = reply.respond(WORKSPACE, chat_id)

    assert message is not None
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.workspace_id == WORKSPACE
    assert request.chat_id == chat_id
    assert request.ideas == PLAIN_IDEAS
    assert "workspace" not in message.body.lower()
    assert "supported" in message.body
    messages = chat.list_messages(WORKSPACE, chat_id)
    anchors = [item for item in messages if item.kind is TranscriptItemKind.WORKFLOW]
    assert len(anchors) == 1
    anchor_data = anchors[0].data
    assert anchor_data is not None
    assert anchor_data["workflowId"] == "hypothesis-development"
    executions = chat.list_executions(WORKSPACE)
    assert [execution.workflow_id for execution in executions] == [
        "hypothesis-development"
    ]


def test_plain_language_idea_workspace_comes_from_active_conversation() -> None:
    chat = ChatService()
    broker = InMemoryChatStreamBroker()
    runtime = FakeDevelopmentRuntime()
    reply = ChatReplyService(
        chat_service=chat,
        broker=broker,
        provider=lambda history: iter(("plain reply",)),
        development_runtime=runtime,
    )
    chat_id = chat.create_chat(
        WORKSPACE, title="Research", owner_actor_id="operator"
    ).chat_id
    chat.create_message(
        WORKSPACE,
        chat_id,
        actor_id="operator",
        body="I have a theory: value stocks mean-revert after drawdowns.",
    )

    reply.respond(WORKSPACE, chat_id)

    assert runtime.requests[0].workspace_id == WORKSPACE
    assert runtime.requests[0].chat_id == chat_id


@pytest.mark.parametrize(
    "body",
    [
        "What is the Sharpe ratio?",
        "Hello there",
        "How do I read a backtest report?",
        "Thanks, that helps.",
        "Show me the backtests",
        json.dumps({"workflow": "hypothesis-validation", "claim": "old trigger"}),
    ],
)
def test_ordinary_qa_never_launches_the_pipeline(body: str) -> None:
    chat = ChatService()
    broker = InMemoryChatStreamBroker()
    runtime = FakeDevelopmentRuntime()
    reply = ChatReplyService(
        chat_service=chat,
        broker=broker,
        provider=lambda history: iter(("plain reply",)),
        development_runtime=runtime,
    )
    chat_id = chat.create_chat(
        WORKSPACE, title="Research", owner_actor_id="operator"
    ).chat_id
    chat.create_message(WORKSPACE, chat_id, actor_id="operator", body=body)

    message = reply.respond(WORKSPACE, chat_id)

    assert message is not None
    assert message.body == "plain reply"
    assert runtime.requests == []
    assert chat.list_executions(WORKSPACE) == ()


def test_retired_validation_seams_are_removed() -> None:
    import thesis.chat.responder as responder
    import thesis.workflows.runtime as runtime_module

    assert not hasattr(responder, "launch_hypothesis_validation")
    assert not hasattr(responder, "ValidationRuntime")
    assert not hasattr(runtime_module.WorkflowRuntime, "build_hypothesis_validation")
    assert not hasattr(runtime_module.WorkflowRuntime, "execute_hypothesis_validation")
