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
from thesis.agent.runtime.datum import tool_result_payload
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
    RecordNotFound,
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
    TranscriptItemKind,
)
from thesis.chat.service import ChatService
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ToolCall

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


def _tool_calls_from(data: Mapping[str, object]) -> tuple[ToolCall, ...]:
    """Return the assistant tool-call envelope persisted on a message."""
    raw = data.get("toolCalls")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        if not isinstance(function, Mapping):
            continue
        call_id = item.get("id")
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(call_id, str) or not isinstance(name, str):
            continue
        calls.append(
            ToolCall(
                id=call_id,
                name=name,
                arguments=arguments if isinstance(arguments, str) else "",
            )
        )
    return tuple(calls)


def _execution_epoch(chat: ChatService, record: SubAgentRecord) -> int:
    """Return the owning execution's current intent epoch, or zero."""
    if record.workflow_execution_id is None:
        return 0
    try:
        execution = chat.get_execution(
            record.workspace_id, record.workflow_execution_id
        )
    except RecordNotFound:
        return 0
    return execution.intent_epoch


def _node_context(
    child: SubAgentRecord,
    *,
    trigger_message_id: str | None = None,
    intent_epoch: int = 0,
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
        intent_epoch=intent_epoch,
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
        context = _node_context(
            running,
            trigger_message_id=task_message.message_id,
            intent_epoch=_execution_epoch(self._chat, running),
        )
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
        paused = self._lifecycle.get(running.workspace_id, running.sub_agent_id)
        if paused is not None and paused.status is SubAgentStatus.NEEDS_INPUT:
            # The turn paused for operator input through the unified node-input
            # path: the node stays alive, its question renders at its own anchor,
            # and the operator's accepted answer resumes this conversation.
            return paused
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
        lifecycle: SubAgentLifecycle | None = None,
        on_fault: Callable[[SubAgentRecord, Exception], None] | None = None,
        delivery: DeliveryService | None = None,
    ) -> None:
        self._chat = chat
        self._store = store
        self._provider = provider
        self._registry_factory = registry_factory
        self._max_tool_iterations = max_tool_iterations
        self._lifecycle = lifecycle
        self._on_fault = on_fault
        self._delivery = delivery

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

    def run(
        self,
        workspace_id: str,
        chat_id: str,
        sub_agent_id: str,
        *,
        pull_new_input: bool = False,
    ) -> SessionResult:
        """Run one node turn and persist its assistant and tool turns.

        ``pull_new_input`` drains messages that arrive while the turn runs into
        the live session between model iterations, so a wake that lands
        mid-turn is seen by the running model instead of waiting for a rerun.
        """
        record = self._store.get_sub_agent(workspace_id, sub_agent_id)
        if record is None or record.chat_id != chat_id:
            raise NodeTurnError(f"sub-agent was not found: {sub_agent_id}")
        if record.status is SubAgentStatus.PENDING and self._lifecycle is not None:
            # The runner starts the agent it runs: spawning created the record
            # and its prompt scheduled this turn, so pending must not survive
            # the turn that consumes that prompt.
            record = self._lifecycle.start(workspace_id, sub_agent_id)
        transcript = self.transcript(workspace_id, chat_id, sub_agent_id)
        trigger_message_id = next(
            (
                message.message_id
                for message in reversed(transcript)
                if message.role is MessageRole.USER
            ),
            None,
        )
        context = _node_context(
            record,
            trigger_message_id=trigger_message_id,
            intent_epoch=_execution_epoch(self._chat, record),
        )
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
        seen_ids = {message.message_id for message in transcript}

        def pull() -> list[ProviderChatMessage]:
            fresh: list[ProviderChatMessage] = []
            for message in self.transcript(workspace_id, chat_id, sub_agent_id):
                if message.message_id in seen_ids:
                    continue
                seen_ids.add(message.message_id)
                body = message.body.strip()
                if _role_name(message.role) == "user" and body:
                    fresh.append(ProviderChatMessage(role="user", content=body))
            return fresh

        try:
            result = session.run(
                self._seed(record, transcript),
                before_model_call=pull if pull_new_input else None,
            )
        except Exception as error:  # noqa: BLE001 - a phase fault fails the node
            message = str(error) or type(error).__name__
            failed = (
                None
                if self._lifecycle is None
                else self._lifecycle.fail(
                    record.workspace_id,
                    record.sub_agent_id,
                    result={"failure": message},
                )
            )
            if self._on_fault is not None:
                self._on_fault(failed or record, error)
            return SessionResult(status="failed", content=message, turns=())
        self._persist(record, result)
        self._finish_one_shot(record, result)
        return result

    def _finish_one_shot(self, record: SubAgentRecord, result: SessionResult) -> None:
        """Complete a chat-spawned one-shot child whose turn ended in text.

        A one-shot child returns its result by its final answer: the turn ends,
        the node completes with that answer, and the result is delivered to the
        caller's anchor.  Phase agents (which carry a phase) complete only
        through the completion tool, and conversation agents stay open.
        """
        if (
            result.status != "completed"
            or record.kind is not SubAgentKind.ONE_SHOT
            or record.phase is not None
            or self._lifecycle is None
        ):
            return
        current = self._store.get_sub_agent(record.workspace_id, record.sub_agent_id)
        if current is None or current.status is not SubAgentStatus.RUNNING:
            return
        completed = self._lifecycle.complete(
            record.workspace_id,
            record.sub_agent_id,
            result={"summary": result.content},
        )
        if self._delivery is not None:
            self._delivery.deliver_child_result(
                Anchor(
                    record.workspace_id,
                    record.chat_id,
                    sub_agent_id=record.parent_sub_agent_id,
                ),
                completed,
            )

    def _seed(
        self, record: SubAgentRecord, transcript: Sequence[ChatMessage]
    ) -> list[ProviderChatMessage]:
        messages: list[ProviderChatMessage] = [
            ProviderChatMessage(role="system", content=_child_system_prompt(record))
        ]
        announced: set[str] = set()
        for message in transcript:
            role = _role_name(message.role)
            body = message.body.strip()
            data = message.data if isinstance(message.data, Mapping) else {}
            if role == "assistant":
                calls = _tool_calls_from(data)
                if calls:
                    announced.update(call.id for call in calls)
                    messages.append(
                        ProviderChatMessage(
                            role="assistant", content=body, tool_calls=calls
                        )
                    )
                    continue
                if body:
                    messages.append(ProviderChatMessage(role="assistant", content=body))
                continue
            if role == "tool":
                payload = (
                    dict(message.tool_result)
                    if message.tool_result is not None
                    else {"ok": True, "text": body}
                )
                content = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
                tool_call_id = data.get("toolCallId")
                if isinstance(tool_call_id, str) and tool_call_id in announced:
                    messages.append(
                        ProviderChatMessage(
                            role="tool", content=content, tool_call_id=tool_call_id
                        )
                    )
                else:
                    # A delivery or unattached result is an inbound note, not a
                    # tool reply; replaying it as a tool message without its
                    # assistant envelope is invalid for the provider.
                    messages.append(
                        ProviderChatMessage(role="user", content=body or content)
                    )
                continue
            if role in _CHAT_ROLES and body:
                messages.append(ProviderChatMessage(role=role, content=body))
        return messages

    def _persist(self, record: SubAgentRecord, result: SessionResult) -> None:
        for turn in result.turns:
            if turn.role == "tool":
                self._persist_tool_turn(record, turn)
            elif turn.role == "assistant" and (turn.content.strip() or turn.tool_calls):
                body = turn.content.strip() or (
                    "Requested tool call(s): "
                    + ", ".join(call.name for call in turn.tool_calls)
                )
                data: dict[str, object] = {
                    "subAgentId": record.sub_agent_id,
                    "turn": "assistant",
                    "invocationId": turn.invocation_id,
                }
                if turn.tool_calls:
                    data["toolCalls"] = [call.as_wire() for call in turn.tool_calls]
                self._chat.create_message(
                    record.workspace_id,
                    record.chat_id,
                    actor_id=record.created_by,
                    body=body,
                    role=MessageRole.ASSISTANT,
                    kind=TranscriptItemKind.SUB_AGENT,
                    data=data,
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
        source = _decoded_payload(turn.content) or {}
        self._chat.create_message(
            record.workspace_id,
            record.chat_id,
            actor_id=record.created_by,
            body=body,
            role=MessageRole.TOOL,
            kind=TranscriptItemKind.SUB_AGENT,
            tool_result=tool_result_payload(
                call_id=turn.tool_call_id,
                ok=source.get("ok"),
                failure=source.get("failure"),
                structured=source.get("structured"),
                summary=source.get("summary"),
                text=source.get("text"),
                tool=turn.tool_name,
                datum_type=None if datum is None else datum.datum_type,
            ),
            data=data,
        )


__all__ = [
    "ChildRunner",
    "NodeTurnError",
    "NodeTurnRunner",
    "RegistryFactory",
]
