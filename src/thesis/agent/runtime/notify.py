"""Asynchronous delivery of a child sub-agent's result to its parent.

A free-form chat must stay responsive while tool and child work runs in the
background.  This module is a thin caller of the unified
:class:`~thesis.agent.runtime.delivery.DeliveryService`: a child's terminal (or
non-terminal) result is delivered by persisting it at the parent's anchor and
scheduling one turn there.  Nothing blocks on a child: the parent can keep
receiving operator messages, and polls with ``await_sub_agent`` only when it
wants an explicit snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from thesis.agent.runtime.delivery import (
    Anchor,
    DeliveryService,
    TurnScheduler,
)
from thesis.chat.contracts import ChatMessage, SubAgentRecord
from thesis.chat.service import ChatService


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
        delivery: DeliveryService | None = None,
    ) -> None:
        self._delivery = delivery or DeliveryService(
            chat, schedule=schedule, clock=clock
        )
        # Retained for signature compatibility; idempotency derives from the
        # datum content, so the notifier never mints its own message ids.
        self._id_factory = id_factory

    def notify(self, child: SubAgentRecord) -> ChatMessage | None:
        """Deliver a durable result notification to the parent's anchor.

        Returns ``None`` for a root sub-agent that has no parent.
        """
        parent_sub_agent_id = child.parent_sub_agent_id
        if parent_sub_agent_id is None:
            return None
        receipt = self._delivery.deliver_child_result(
            Anchor(
                child.workspace_id,
                child.chat_id,
                sub_agent_id=parent_sub_agent_id,
            ),
            child,
        )
        return receipt.message


__all__ = [
    "SubAgentNotifier",
    "TurnScheduler",
]
