"""Asynchronous delivery of a child sub-agent's result to its parent.

A free-form chat must stay responsive while tool and child work runs in the
background.  This module delivers a child's terminal (or non-terminal) result by
appending a durable ``tool`` message to the parent's conversation and then
scheduling a new parent turn through an injected scheduler.  Nothing blocks on a
child: the parent can keep receiving operator messages, and polls with
``await_sub_agent`` only when it wants an explicit snapshot.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from thesis.chat.contracts import (
    ChatMessage,
    MessageRole,
    SubAgentRecord,
    TranscriptItemKind,
)
from thesis.chat.service import ChatService

#: Scheduler signature: (workspace_id, chat_id, sub_agent_id) -> None.
TurnScheduler = Callable[[str, str, str], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SubAgentNotifier:
    """Deliver child results to parents durably and asynchronously."""

    def __init__(
        self,
        chat: ChatService,
        *,
        schedule: TurnScheduler | None = None,
        id_factory: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._chat = chat
        self._schedule = schedule
        self._id_factory = id_factory or (lambda: f"notice-{uuid.uuid4().hex}")
        self._clock = clock

    def notify(self, child: SubAgentRecord) -> ChatMessage | None:
        """Append a durable result notification to the parent and schedule it.

        Returns ``None`` for a root sub-agent that has no parent.
        """
        parent_sub_agent_id = child.parent_sub_agent_id
        if parent_sub_agent_id is None:
            return None
        body = self._body(child)
        message = self._chat.create_message(
            child.workspace_id,
            child.chat_id,
            actor_id="thesis-runtime",
            body=body,
            role=MessageRole.TOOL,
            kind=TranscriptItemKind.SUB_AGENT,
            execution_id=child.workflow_execution_id,
            data={
                "subAgentId": parent_sub_agent_id,
                "childSubAgentId": child.sub_agent_id,
                "kind": "child_result",
                "status": child.status.value,
                "result": None if child.result is None else dict(child.result),
                "resultRef": child.result_ref,
            },
        )
        if self._schedule is not None:
            self._schedule(child.workspace_id, child.chat_id, parent_sub_agent_id)
        return message

    @staticmethod
    def _body(child: SubAgentRecord) -> str:
        label = child.role or child.sub_agent_id
        return f"Sub-agent {label} ({child.sub_agent_id}) ended: {child.status.value}."


__all__ = [
    "SubAgentNotifier",
    "TurnScheduler",
]
