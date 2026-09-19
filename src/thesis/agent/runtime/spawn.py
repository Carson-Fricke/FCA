"""Spawn an agent and give it its first prompt in one durable operation.

A caller never creates an agent and then separately remembers to hand it a
prompt: :meth:`SubAgentSpawner.spawn` creates the durable record and delivers
the first message through the one delivery path, so the delivery both persists
the prompt and wakes the agent's own turn.  "Prompted" and "started" are the
same act; there is no window in which an agent exists with nothing to do.

The delivery is idempotent under its key, so a retried spawn resolves to the
same agent and the same first message instead of duplicating either.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from thesis.agent.runtime.datum import Datum, DatumType
from thesis.agent.runtime.delivery import (
    Anchor,
    DeliveryReceipt,
    DeliveryService,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.chat.contracts import MessageRole, SubAgentRecord

__all__ = ["SpawnedAgent", "SubAgentSpawner"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SpawnedAgent:
    """The durable result of spawning one agent with its first prompt."""

    record: SubAgentRecord
    delivery: DeliveryReceipt | None

    @property
    def sub_agent_id(self) -> str:
        """Return the spawned agent's id."""
        return self.record.sub_agent_id


class SubAgentSpawner:
    """The single public way to create an agent: spawn it with its prompt."""

    def __init__(
        self,
        *,
        lifecycle: SubAgentLifecycle,
        delivery: DeliveryService,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._lifecycle = lifecycle
        self._delivery = delivery
        self._clock = clock

    def spawn(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        spec: SubAgentSpec,
        actor_id: str,
        prompt: str,
        parent_sub_agent_id: str | None = None,
        spawned_by_invocation_id: str | None = None,
        workflow_execution_id: str | None = None,
        phase_attempt_id: str | None = None,
        sub_agent_id: str | None = None,
        prompt_key: str | None = None,
        prompt_data: Mapping[str, object] | None = None,
    ) -> SpawnedAgent:
        """Create the agent and deliver its first prompt, waking its own turn.

        ``sub_agent_id`` reopens an existing durable agent (its identity,
        parent, and transcript are preserved) instead of creating a new one;
        the prompt delivery is idempotent under ``prompt_key``, so a reopen
        that already has its seed neither duplicates the message nor wakes a
        turn twice.
        """
        record = self._lifecycle.spawn(
            workspace_id,
            chat_id,
            spec=spec,
            actor_id=actor_id,
            parent_sub_agent_id=parent_sub_agent_id,
            spawned_by_invocation_id=spawned_by_invocation_id,
            workflow_execution_id=workflow_execution_id,
            phase_attempt_id=phase_attempt_id,
            sub_agent_id=sub_agent_id,
        )
        delivery = self.deliver_prompt(
            workspace_id,
            chat_id,
            record,
            prompt,
            key=prompt_key,
            data=prompt_data,
        )
        return SpawnedAgent(record=record, delivery=delivery)

    def deliver_prompt(
        self,
        workspace_id: str,
        chat_id: str,
        record: SubAgentRecord,
        prompt: str,
        *,
        key: str | None = None,
        data: Mapping[str, object] | None = None,
    ) -> DeliveryReceipt:
        """Deliver one agent's prompt at its own anchor and wake its turn."""
        body = prompt.strip()
        if not body:
            raise ValueError("a spawned agent requires a non-empty prompt")
        payload: dict[str, object] = {
            "subAgentId": record.sub_agent_id,
            "kind": "agent_prompt",
        }
        if data is not None:
            payload.update(dict(data))
        return self._delivery.deliver(
            Anchor(workspace_id, chat_id, sub_agent_id=record.sub_agent_id),
            Datum(
                datum_type=DatumType.AGENT_PROMPT.value,
                ok=True,
                structured=payload,
                text=body,
                summary=body,
            ),
            body=body,
            role=MessageRole.USER,
            data=payload,
            execution_id=record.workflow_execution_id,
            actor_id=record.created_by,
            idempotency_key=key,
            schedule=True,
        )
