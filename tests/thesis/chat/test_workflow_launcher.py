"""The workflow-launch adapter binds the anchor, trigger, and investigation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from thesis.chat.responder import DevelopmentRuntime
from thesis.chat.service import ChatService, InMemoryChatPersistence
from thesis.chat.workflow_launcher import AnchorPublisher, RuntimeWorkflowLauncher

WORKSPACE = "ws-test"
CHAT = "chat-1"
ACTOR = "operator-1"


class _Workflow:
    def __init__(self, request: object) -> None:
        self._request = request
        self.launches: list[dict[str, object]] = []

    def launch(self, *args: object, **kwargs: object) -> object:
        del args
        self.launches.append(dict(kwargs))
        return self._request


class _Runtime:
    """Minimal development runtime returning one running outcome."""

    def __init__(self) -> None:
        self.launched: list[dict[str, object]] = []
        self.request = SimpleNamespace(execution_id="exec-1")
        self.outcome = SimpleNamespace(
            execution_id="exec-1",
            phase=SimpleNamespace(value="requested"),
            verdict=None,
            detail=None,
        )
        self.workflow = _Workflow(self.request)

    def build_hypothesis_development(self, *, chat: object) -> _Workflow:
        del chat
        return self.workflow

    def execute_hypothesis_development(
        self, request: object, *, chat: object
    ) -> object:
        del chat
        self.launched.append({"request": request})
        return self.outcome


def _launcher(
    runtime: _Runtime,
    chat: ChatService,
    *,
    publish_anchor: AnchorPublisher | None = None,
) -> RuntimeWorkflowLauncher:
    return RuntimeWorkflowLauncher(
        cast(DevelopmentRuntime, runtime), chat, publish_anchor=publish_anchor
    )


def test_unknown_investigation_is_dropped_instead_of_aborting() -> None:
    runtime = _Runtime()
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat.create_chat(WORKSPACE, title="chat", owner_actor_id=ACTOR)
    launcher = _launcher(runtime, chat)

    result = launcher.start_hypothesis_development(
        workspace_id=WORKSPACE,
        chat_id=CHAT,
        actor_id=ACTOR,
        ideas="MSTR mean-reverts intraday",
        investigation_id="mstr-ibit-intraday-mean-reversion",
        trigger_message_id="message-1",
    )

    assert result["executionId"] == "exec-1"
    warnings = result["warnings"]
    assert isinstance(warnings, list) and warnings
    assert "was not found" in str(warnings[0])


def test_known_investigation_is_linked_without_a_warning() -> None:
    runtime = _Runtime()
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(WORKSPACE, title="chat", owner_actor_id=ACTOR)
    investigation = chat.create_investigation(
        WORKSPACE,
        title="MSTR/IBIT",
        objective="Test intraday mean reversion",
        owner_actor_id=ACTOR,
    )
    launcher = _launcher(runtime, chat)

    result = launcher.start_hypothesis_development(
        workspace_id=WORKSPACE,
        chat_id=thread.chat_id,
        actor_id=ACTOR,
        ideas="MSTR mean-reverts intraday",
        investigation_id=investigation.investigation_id,
        trigger_message_id="message-1",
    )

    assert result["warnings"] == []


def test_launcher_threads_the_anchor_and_trigger_message() -> None:
    runtime = _Runtime()
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat.create_chat(WORKSPACE, title="chat", owner_actor_id=ACTOR)
    published: list[tuple[str, str, str]] = []
    launcher = _launcher(
        runtime,
        chat,
        publish_anchor=lambda workspace, chat_id, execution: published.append(
            (workspace, chat_id, execution)
        ),
    )

    launcher.start_hypothesis_development(
        workspace_id=WORKSPACE,
        chat_id=CHAT,
        actor_id=ACTOR,
        ideas="MSTR mean-reverts intraday",
        trigger_message_id="message-9",
        anchor_sub_agent_id="subagent-7",
    )

    launch = runtime.workflow.launches[0]
    assert launch["trigger_message_id"] == "message-9"
    assert launch["anchor_sub_agent_id"] == "subagent-7"
    assert published == [(WORKSPACE, CHAT, "exec-1")]


def test_launcher_requires_the_requesting_message() -> None:
    runtime = _Runtime()
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat.create_chat(WORKSPACE, title="chat", owner_actor_id=ACTOR)
    launcher = _launcher(runtime, chat)

    with pytest.raises(ValueError, match="requesting message"):
        launcher.start_hypothesis_development(
            workspace_id=WORKSPACE,
            chat_id=CHAT,
            actor_id=ACTOR,
            ideas="MSTR mean-reverts intraday",
        )

    assert runtime.workflow.launches == []
