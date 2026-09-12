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
from thesis.chat.contracts import MessageRole
from thesis.execution.events import ExecutionEventType
from thesis.workflows.hypothesis_development import (
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    FalsificationVerdict,
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentPhase,
    HypothesisDevelopmentRequest,
)
from thesis.workflows.hypothesis_validation import VERDICT_SUPPORTED

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
    ) -> HypothesisDevelopmentRequest:
        execution = self._chat.launch_workflow(
            workspace_id,
            chat_id,
            workflow=HYPOTHESIS_DEVELOPMENT_WORKFLOW,
            actor_id=actor_id,
            body=ideas,
            investigation_id=investigation_id,
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
