"""The workflow-launch adapter binds the anchor, trigger, and investigation.

Launch is asynchronous: the tool result is the execution handle and the
workflow runs detached, delivering its outcome at the caller's anchor.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest

from thesis.chat.responder import DevelopmentRuntime
from thesis.chat.service import ChatService, InMemoryChatPersistence
from thesis.chat.workflow_launcher import (
    AnchorPublisher,
    Detach,
    RuntimeWorkflowLauncher,
)
from thesis.workflows.hypothesis_development import HYPOTHESIS_DEVELOPMENT_WORKFLOW

WORKSPACE = "ws-test"
ACTOR = "operator-1"


class _Workflow:
    """Fake launch surface that creates a real durable execution."""

    def __init__(self, chat: ChatService) -> None:
        self._chat = chat
        self._request: object | None = None
        self.launches: list[dict[str, object]] = []

    @property
    def request(self) -> object:
        assert self._request is not None
        return self._request

    def launch(self, workspace_id: str, chat_id: str, **kwargs: object) -> object:
        self.launches.append(dict(kwargs))
        actor_id = kwargs["actor_id"]
        trigger_message_id = kwargs["trigger_message_id"]
        assert isinstance(actor_id, str) and isinstance(trigger_message_id, str)
        execution = self._chat.launch_workflow(
            workspace_id,
            chat_id,
            workflow=HYPOTHESIS_DEVELOPMENT_WORKFLOW,
            actor_id=actor_id,
            message_id=trigger_message_id,
        )
        self._request = SimpleNamespace(execution_id=execution.execution_id)
        return self._request


class _Runtime:
    """Minimal development runtime returning one running outcome."""

    def __init__(self, chat: ChatService) -> None:
        self.launched: list[dict[str, object]] = []
        self.workflow = _Workflow(chat)
        self.outcome = SimpleNamespace(
            execution_id="exec-1",
            phase=SimpleNamespace(value="requested"),
            verdict=None,
            detail=None,
        )
        self._execution_id: str | None = None

    def build_hypothesis_development(self, *, chat: object) -> _Workflow:
        del chat
        return self.workflow

    def execute_hypothesis_development(
        self, request: object, *, chat: object
    ) -> object:
        del chat
        execution_id = cast(SimpleNamespace, request).execution_id
        self._execution_id = execution_id
        self.launched.append({"request": request})
        return SimpleNamespace(
            execution_id=execution_id,
            phase=self.outcome.phase,
            verdict=self.outcome.verdict,
            detail=self.outcome.detail,
        )


def _setup() -> tuple[_Runtime, ChatService, str, str]:
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(WORKSPACE, title="chat", owner_actor_id=ACTOR)
    trigger = chat.create_message(
        WORKSPACE, thread.chat_id, actor_id=ACTOR, body="develop this idea"
    )
    return _Runtime(chat), chat, thread.chat_id, trigger.message_id


def _launcher(
    runtime: _Runtime,
    chat: ChatService,
    *,
    publish_anchor: AnchorPublisher | None = None,
    detach: Detach | None = None,
) -> RuntimeWorkflowLauncher:
    return RuntimeWorkflowLauncher(
        cast(DevelopmentRuntime, runtime),
        chat,
        publish_anchor=publish_anchor,
        detach=detach,
    )


def test_unknown_investigation_is_dropped_instead_of_aborting() -> None:
    runtime, chat, chat_id, trigger = _setup()
    launcher = _launcher(runtime, chat, detach=lambda function: None)

    result = launcher.start_hypothesis_development(
        workspace_id=WORKSPACE,
        chat_id=chat_id,
        actor_id=ACTOR,
        ideas="MSTR mean-reverts intraday",
        investigation_id="mstr-ibit-intraday-mean-reversion",
        trigger_message_id=trigger,
    )

    assert result["executionId"] == runtime.workflow.request.execution_id
    assert result["phase"] == "requested"
    warnings = result["warnings"]
    assert isinstance(warnings, list) and warnings
    assert "was not found" in str(warnings[0])


def test_known_investigation_is_linked_without_a_warning() -> None:
    runtime, chat, chat_id, trigger = _setup()
    investigation = chat.create_investigation(
        WORKSPACE,
        title="MSTR/IBIT",
        objective="Test intraday mean reversion",
        owner_actor_id=ACTOR,
    )
    launcher = _launcher(runtime, chat, detach=lambda function: None)

    result = launcher.start_hypothesis_development(
        workspace_id=WORKSPACE,
        chat_id=chat_id,
        actor_id=ACTOR,
        ideas="MSTR mean-reverts intraday",
        investigation_id=investigation.investigation_id,
        trigger_message_id=trigger,
    )

    assert result["warnings"] == []


def test_launcher_threads_the_anchor_and_trigger_message() -> None:
    runtime, chat, chat_id, trigger = _setup()
    published: list[tuple[str, str, str]] = []
    launcher = _launcher(
        runtime,
        chat,
        publish_anchor=lambda workspace, chat_id, execution: published.append(
            (workspace, chat_id, execution)
        ),
        detach=lambda function: None,
    )

    launcher.start_hypothesis_development(
        workspace_id=WORKSPACE,
        chat_id=chat_id,
        actor_id=ACTOR,
        ideas="MSTR mean-reverts intraday",
        trigger_message_id=trigger,
        anchor_sub_agent_id="subagent-7",
    )

    launch = runtime.workflow.launches[0]
    assert launch["trigger_message_id"] == trigger
    assert launch["anchor_sub_agent_id"] == "subagent-7"
    assert published == [(WORKSPACE, chat_id, runtime.workflow.request.execution_id)]


def test_launcher_requires_the_requesting_message() -> None:
    runtime, chat, chat_id, _ = _setup()
    launcher = _launcher(runtime, chat, detach=lambda function: None)

    with pytest.raises(ValueError, match="requesting message"):
        launcher.start_hypothesis_development(
            workspace_id=WORKSPACE,
            chat_id=chat_id,
            actor_id=ACTOR,
            ideas="MSTR mean-reverts intraday",
        )

    assert runtime.workflow.launches == []


def test_launcher_returns_before_the_workflow_executes() -> None:
    runtime, chat, chat_id, trigger = _setup()
    detached: list[Callable[[], None]] = []
    launcher = _launcher(runtime, chat, detach=detached.append)

    result = launcher.start_hypothesis_development(
        workspace_id=WORKSPACE,
        chat_id=chat_id,
        actor_id=ACTOR,
        ideas="MSTR mean-reverts intraday",
        trigger_message_id=trigger,
    )

    assert result["executionId"] == runtime.workflow.request.execution_id
    assert result["verdict"] is None
    assert runtime.launched == []
    assert len(detached) == 1

    detached[0]()

    assert len(runtime.launched) == 1


def test_launcher_delivers_the_outcome_at_the_root_anchor() -> None:
    runtime, chat, chat_id, trigger = _setup()
    scheduled: list[tuple[str, str, str]] = []
    launcher = RuntimeWorkflowLauncher(
        cast(DevelopmentRuntime, runtime),
        chat,
        detach=lambda function: function(),
        turn_scheduler=lambda workspace, chat, node: scheduled.append(
            (workspace, chat, node)
        ),
    )

    launcher.start_hypothesis_development(
        workspace_id=WORKSPACE,
        chat_id=chat_id,
        actor_id=ACTOR,
        ideas="MSTR mean-reverts intraday",
        trigger_message_id=trigger,
    )

    delivered = [
        message
        for message in chat.list_messages(WORKSPACE, chat_id)
        if message.data is not None and message.data.get("kind") == "workflow_outcome"
    ]
    assert len(delivered) == 1
    assert delivered[0].data is not None
    assert delivered[0].data["datumType"] == "workflow_execution"
    assert delivered[0].data["executionId"] == runtime.workflow.request.execution_id
    assert delivered[0].execution_id == runtime.workflow.request.execution_id
    assert scheduled == [(WORKSPACE, chat_id, "")]
