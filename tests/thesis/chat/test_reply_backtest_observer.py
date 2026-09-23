"""Focused coverage for the optional chat tool-result observer hook.

The observer runs immediately before a tool result is persisted, so a caller
can enrich the persisted tool message (for example with the workspace
``backtest_id`` imported from a finished harness run) without changing the
default reply behavior.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from typing import cast

from thesis.agent.runtime.conversation import Conversations
from thesis.chat import (
    ChatMessage,
    ChatService,
    InMemoryChatStreamBroker,
)
from thesis.chat.contracts import MessageRole
from thesis.chat.responder import ToolResultChunk

WORKSPACE = "workspace-a"


def _thread(service: ChatService):
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body="run it")
    return chat


def _conversations(
    service: ChatService,
    broker: InMemoryChatStreamBroker,
    stream,
    *,
    tool_result_observer=None,
) -> Conversations:
    return Conversations(
        chat=service,
        broker=broker,
        reply_for=lambda workspace_id, chat_id, history: stream(history),
        tool_result_observer=tool_result_observer,
    )


def test_observer_enrichment_is_persisted_on_the_tool_message() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = _thread(service)
    seen: list[tuple[str, str, str | None]] = []

    def stream(
        history: Sequence[ChatMessage],
    ) -> Iterator[str | ToolResultChunk]:
        del history
        yield ToolResultChunk(
            tool="run_backtest",
            call_id="call-1",
            ok=True,
            summary="run_backtest completed",
            structured={"boundary": "local", "run_id": "run-1", "state": "running"},
        )
        yield "Done."

    def observer(
        workspace_id: str,
        chat_id: str,
        execution_id: str | None,
        chunk: ToolResultChunk,
    ) -> ToolResultChunk:
        seen.append((workspace_id, chat_id, execution_id))
        return replace(
            chunk,
            structured={
                **dict(chunk.structured),
                "backtest_id": "bt-001",
                "state": "complete",
            },
        )

    conversations = _conversations(
        service, broker, stream, tool_result_observer=observer
    )

    message = conversations.root(WORKSPACE, chat.chat_id).run_turn()

    assert message is not None
    assert seen == [(WORKSPACE, chat.chat_id, None)]
    tool_message = next(
        item
        for item in service.list_messages(WORKSPACE, chat.chat_id)
        if item.role is MessageRole.TOOL
    )
    assert tool_message.tool_result is not None
    structured = cast(Mapping[str, object], tool_message.tool_result["structured"])
    assert structured["backtest_id"] == "bt-001"
    assert structured["run_id"] == "run-1"
    assert structured["state"] == "complete"


def test_observer_fault_does_not_break_the_reply() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = _thread(service)

    def stream(
        history: Sequence[ChatMessage],
    ) -> Iterator[str | ToolResultChunk]:
        del history
        yield ToolResultChunk(
            tool="get_run",
            call_id="call-1",
            ok=True,
            summary="get_run completed",
            structured={"run_id": "run-1"},
        )
        yield "Recovered."

    def observer(
        workspace_id: str,
        chat_id: str,
        execution_id: str | None,
        chunk: ToolResultChunk,
    ) -> ToolResultChunk:
        raise RuntimeError("observer exploded")

    conversations = _conversations(
        service, broker, stream, tool_result_observer=observer
    )

    message = conversations.root(WORKSPACE, chat.chat_id).run_turn()

    assert isinstance(message, ChatMessage)
    assert message.body == "Recovered."
    tool_message = next(
        item
        for item in service.list_messages(WORKSPACE, chat.chat_id)
        if item.role is MessageRole.TOOL
    )
    assert tool_message.tool_result is not None
    assert tool_message.tool_result["structured"] == {"run_id": "run-1"}


def test_observer_is_optional_and_defaults_to_no_op() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = _thread(service)

    conversations = _conversations(service, broker, lambda history: iter(["Plain."]))

    message = conversations.root(WORKSPACE, chat.chat_id).run_turn()

    assert isinstance(message, ChatMessage)
    assert message.body == "Plain."
