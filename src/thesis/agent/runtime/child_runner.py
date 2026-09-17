"""Execute chat-spawned sub-agents and deliver their results to the caller.

A spawned child owns its own durable conversation and runs its own
:class:`~thesis.agent.runtime.session.AgentSession` turn loop with the child's
effective allowed-tool policy.  The parent turn never blocks: the child runs
after the spawn command returns, and its terminal result is delivered through
the unified :class:`~thesis.agent.runtime.delivery.DeliveryService`.

:class:`NodeTurnRunner` is the other half: when a datum or an operator message
arrives at a node, it rebuilds that node's transcript from the node's own
anchored messages, runs one session turn, and persists the assistant and tool
turns as node-scoped transcript messages.  Sub-agent turns are therefore a real
transcript, not only invocation records.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.delivery import (
    DELIVERY_KEY_FIELD,
    Anchor,
    DeliveryService,
    TurnScheduler,
    find_delivered,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.session import (
    DEFAULT_MAX_TOOL_ITERATIONS,
    AgentSession,
    CompletionProvider,
    SessionResult,
    SessionToolRegistry,
    SessionTurn,
)
from thesis.agent.runtime.store import SubAgentStore
from thesis.chat.contracts import (
    ChatMessage,
    MessageRole,
    SubAgentRecord,
    TranscriptItemKind,
)
from thesis.chat.service import ChatService
from thesis.providers.invocations import ChatMessage as ProviderChatMessage

#: Builds the merged, policy-scoped tool registry for one child context.
RegistryFactory = Callable[[ToolContext], SessionToolRegistry]

_CHAT_ROLES = frozenset({"user", "assistant", "system"})


class NodeTurnError(RuntimeError):
    """Raised when a node turn cannot be run for the requested node."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _child_system_prompt(child: SubAgentRecord) -> str:
    return (
        f"You are the '{child.role}' sub-agent. Complete the assigned task "
        "using the tools available to you. When the task is complete, "
        "respond with a concise final answer."
    )


def _role_name(role: object) -> str:
    value = getattr(role, "value", role)
    return value if isinstance(value, str) else str(role)


def _decoded_payload(content: str) -> Mapping[str, object] | None:
    try:
        decoded = json.loads(content)
    except ValueError:
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _node_context(
    child: SubAgentRecord, *, trigger_message_id: str | None = None
) -> ToolContext:
    return ToolContext(
        workspace_id=child.workspace_id,
        chat_id=child.chat_id,
        conversation_id=child.sub_agent_id,
        actor_id=child.created_by,
        sub_agent_id=child.sub_agent_id,
        parent_sub_agent_id=child.parent_sub_agent_id,
        workflow_execution_id=child.workflow_execution_id,
        phase_attempt_id=child.phase_attempt_id,
        trigger_message_id=trigger_message_id,
        allowed_tools=child.allowed_tools,
    )


class ChildRunner:
    """Run a spawned child session and deliver its terminal result."""

    def __init__(
        self,
        *,
        chat: ChatService,
        store: SubAgentStore,
        lifecycle: SubAgentLifecycle,
        provider: CompletionProvider,
        registry_factory: RegistryFactory,
        schedule: TurnScheduler | None = None,
        delivery: DeliveryService | None = None,
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._chat = chat
        self._store = store
        self._lifecycle = lifecycle
        self._provider = provider
        self._registry_factory = registry_factory
        self._schedule = schedule
        self._delivery = delivery or DeliveryService(chat, schedule=schedule)
        self._max_tool_iterations = max_tool_iterations
        self._clock = clock

    def run(self, child: SubAgentRecord, task: str) -> SubAgentRecord:
        """Run one child to a terminal state and deliver its result."""
        running = self._lifecycle.start(child.workspace_id, child.sub_agent_id)
        task_message = self._task_message(running, task)
        context = _node_context(running, trigger_message_id=task_message.message_id)
        session = AgentSession(
            workspace_id=running.workspace_id,
            chat_id=running.chat_id,
            conversation_id=running.sub_agent_id,
            provider=self._provider,
            registry=self._registry_factory(context),
            store=self._store,
            sub_agent_id=running.sub_agent_id,
            workflow_execution_id=running.workflow_execution_id,
            phase_attempt_id=running.phase_attempt_id,
            actor_id=running.created_by,
            role="primary",
            max_tool_iterations=self._max_tool_iterations,
        )
        messages = [
            ProviderChatMessage(role="system", content=_child_system_prompt(running)),
            ProviderChatMessage(role="user", content=task),
        ]
        try:
            result = session.run(messages)
        except Exception as error:  # noqa: BLE001 - a child fault is terminal
            terminal = self._lifecycle.fail(
                running.workspace_id,
                running.sub_agent_id,
                result={"failure": str(error) or type(error).__name__},
            )
            self._deliver(terminal)
            return terminal
        if result.status == "completed":
            terminal = self._lifecycle.complete(
                running.workspace_id,
                running.sub_agent_id,
                result={"summary": result.content},
            )
        else:
            terminal = self._lifecycle.fail(
                running.workspace_id,
                running.sub_agent_id,
                result={"failure": result.status},
            )
        self._deliver(terminal)
        return terminal

    def _task_message(self, child: SubAgentRecord, task: str) -> ChatMessage:
        """Persist the child's own task turn and return its durable message.

        The task is the child conversation's first user message, so its id is
        the trigger a child-initiated workflow launch binds to.  A retried run
        resolves to the existing message instead of duplicating the turn.
        """
        key = f"child-task-{child.sub_agent_id}"
        anchor = Anchor(
            child.workspace_id, child.chat_id, sub_agent_id=child.sub_agent_id
        )
        existing = find_delivered(self._chat, anchor, key)
        if existing is not None:
            return existing
        return self._chat.create_message(
            child.workspace_id,
            child.chat_id,
            actor_id=child.created_by,
            body=task,
            role=MessageRole.USER,
            kind=TranscriptItemKind.SUB_AGENT,
            data={
                "subAgentId": child.sub_agent_id,
                "kind": "child_task",
                DELIVERY_KEY_FIELD: key,
            },
        )

    def _deliver(self, child: SubAgentRecord) -> ChatMessage | None:
        receipt = self._delivery.deliver_child_result(
            Anchor(
                child.workspace_id,
                child.chat_id,
                sub_agent_id=child.parent_sub_agent_id,
            ),
            child,
        )
        return receipt.message


class NodeTurnRunner:
    """Run one sub-agent node turn from its own anchored transcript.

    The runner is driven by injected ports only: the completion provider and the
    registry factory stand in for a live model, so the mechanism is testable
    without one.  The scheduler callback is the seam that invokes it.
    """

    def __init__(
        self,
        *,
        chat: ChatService,
        store: SubAgentStore,
        provider: CompletionProvider,
        registry_factory: RegistryFactory,
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
    ) -> None:
        self._chat = chat
        self._store = store
        self._provider = provider
        self._registry_factory = registry_factory
        self._max_tool_iterations = max_tool_iterations

    def transcript(
        self, workspace_id: str, chat_id: str, sub_agent_id: str
    ) -> tuple[ChatMessage, ...]:
        """Return the node's own anchored messages, in conversation order."""
        return tuple(
            message
            for message in self._chat.list_messages(workspace_id, chat_id)
            if message.data is not None
            and message.data.get("subAgentId") == sub_agent_id
        )

    def run(self, workspace_id: str, chat_id: str, sub_agent_id: str) -> SessionResult:
        """Run one node turn and persist its assistant and tool turns."""
        record = self._store.get_sub_agent(workspace_id, sub_agent_id)
        if record is None or record.chat_id != chat_id:
            raise NodeTurnError(f"sub-agent was not found: {sub_agent_id}")
        transcript = self.transcript(workspace_id, chat_id, sub_agent_id)
        trigger_message_id = next(
            (
                message.message_id
                for message in reversed(transcript)
                if message.role is MessageRole.USER
            ),
            None,
        )
        context = _node_context(record, trigger_message_id=trigger_message_id)
        session = AgentSession(
            workspace_id=record.workspace_id,
            chat_id=record.chat_id,
            conversation_id=record.sub_agent_id,
            provider=self._provider,
            registry=self._registry_factory(context),
            store=self._store,
            sub_agent_id=record.sub_agent_id,
            workflow_execution_id=record.workflow_execution_id,
            phase_attempt_id=record.phase_attempt_id,
            actor_id=record.created_by,
            role="primary",
            max_tool_iterations=self._max_tool_iterations,
        )
        result = session.run(self._seed(record, transcript))
        self._persist(record, result)
        return result

    def _seed(
        self, record: SubAgentRecord, transcript: Sequence[ChatMessage]
    ) -> list[ProviderChatMessage]:
        messages: list[ProviderChatMessage] = [
            ProviderChatMessage(role="system", content=_child_system_prompt(record))
        ]
        for message in transcript:
            role = _role_name(message.role)
            body = message.body.strip()
            if role == "tool":
                payload = (
                    dict(message.tool_result)
                    if message.tool_result is not None
                    else {"ok": True, "text": body}
                )
                tool_call_id = str(
                    (message.data or {}).get("toolCallId") or message.message_id
                )
                messages.append(
                    ProviderChatMessage(
                        role="tool",
                        content=json.dumps(
                            payload, ensure_ascii=True, separators=(",", ":")
                        ),
                        tool_call_id=tool_call_id,
                    )
                )
            elif role in _CHAT_ROLES and body:
                messages.append(ProviderChatMessage(role=role, content=body))
        return messages

    def _persist(self, record: SubAgentRecord, result: SessionResult) -> None:
        for turn in result.turns:
            if turn.role == "tool":
                self._persist_tool_turn(record, turn)
            elif turn.role == "assistant" and turn.content.strip():
                self._chat.create_message(
                    record.workspace_id,
                    record.chat_id,
                    actor_id=record.created_by,
                    body=turn.content,
                    role=MessageRole.ASSISTANT,
                    kind=TranscriptItemKind.SUB_AGENT,
                    data={
                        "subAgentId": record.sub_agent_id,
                        "turn": "assistant",
                        "invocationId": turn.invocation_id,
                    },
                )

    def _persist_tool_turn(self, record: SubAgentRecord, turn: SessionTurn) -> None:
        datum = turn.datums[0] if turn.datums else None
        data: dict[str, object] = {
            "subAgentId": record.sub_agent_id,
            "turn": "tool",
            "toolCallId": turn.tool_call_id,
            "invocationId": turn.invocation_id,
        }
        if turn.tool_name is not None:
            data["tool"] = turn.tool_name
        if datum is not None:
            data["datumType"] = datum.datum_type
        summary = ""
        if datum is not None:
            summary = (datum.summary or "").strip() or datum.text.strip()
        body = summary or f"{turn.tool_name or 'tool'} result"
        self._chat.create_message(
            record.workspace_id,
            record.chat_id,
            actor_id=record.created_by,
            body=body,
            role=MessageRole.TOOL,
            kind=TranscriptItemKind.SUB_AGENT,
            tool_result=_decoded_payload(turn.content),
            data=data,
        )


__all__ = [
    "ChildRunner",
    "NodeTurnError",
    "NodeTurnRunner",
    "RegistryFactory",
]
