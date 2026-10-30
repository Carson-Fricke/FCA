"""Spawn with ordered, durable context and one runnable assignment.

Context and assignment share canonical delivery. Context source identities
deduplicate copies across retries/revisits; only the final assignment admits a
turn. Lifecycle owns records and guards, while the conversation service owns
canonical intake, scheduling and provider projection.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from thesis_runtime.contracts.datum import Datum, DatumType
from thesis_runtime.contracts.records import CompletionMode, MessageRole, SubAgentRecord
from thesis_runtime.conversations.delivery import (
    Anchor,
    DeliveryReceipt,
    DeliveryService,
)
from thesis_runtime.subagents.lifecycle import SubAgentLifecycle
from thesis_runtime.subagents.specs import InitialContext, SubAgentSpec

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
        completion_mode: CompletionMode | None = None,
        returns_to_sub_agent_id: str | None = None,
        phase_executor: bool = False,
        initial_context: Sequence[InitialContext] = (),
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
            completion_mode=completion_mode,
            returns_to_sub_agent_id=returns_to_sub_agent_id,
            phase_executor=phase_executor,
        )
        delivery = self.deliver_prompt(
            workspace_id,
            chat_id,
            record,
            prompt,
            key=prompt_key,
            data=prompt_data,
            initial_context=initial_context,
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
        initial_context: Sequence[InitialContext] = (),
    ) -> DeliveryReceipt:
        """Deliver one agent's prompt at its own anchor and wake its turn."""
        body = prompt.strip()
        if not body:
            raise ValueError("a spawned agent requires a non-empty prompt")
        # Context deliveries cannot admit a runnable turn. Only the final
        # assignment does so, after all context is durable. Stable source keys
        # also repair an interrupted startup without copying context on revisit.
        for context in initial_context:
            self._delivery.deliver(
                Anchor(workspace_id, chat_id, sub_agent_id=record.sub_agent_id),
                Datum(
                    "agent_context",
                    ok=True,
                    structured={
                        "sourceMessageId": context.source_message_id,
                        "sourceConversationId": context.source_conversation_id,
                    },
                    summary="Original request delivered.",
                ),
                body=context.text,
                role=MessageRole.USER,
                actor_id=record.created_by,
                execution_id=record.workflow_execution_id,
                idempotency_key=f"initial-context:{context.source_conversation_id}:{context.source_message_id}",
                schedule=False,
            )
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
                summary="Agent instructions delivered.",
            ),
            body=body,
            role=MessageRole.USER,
            data=payload,
            execution_id=record.workflow_execution_id,
            actor_id=record.created_by,
            idempotency_key=key,
            schedule=True,
        )
