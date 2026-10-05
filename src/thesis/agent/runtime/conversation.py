"""One conversation: an anchored transcript and its serialized turns.

A conversation is the only thing that owns a transcript and turns.  Its
identity is one conversation id — the chat id for a conversation at the root
anchor, the sub-agent id for a nested one — plus its workspace and kind.  Its
transcript is anchored: rows in the containing chat's shared message store,
discriminated by anchor, so every conversation in a chat streams on that
chat's unified timeline.  The chat is containment and storage scope, never a
second identity.

Operations: ``send(body, actor)`` and ``deliver(datum)`` each persist then
wake; ``request_input`` pauses the node for an operator reply; ``status`` and
``in_flight`` observe; ``subscribe`` tails the chat's live frames; ``run_turn``
is the turn body the worker calls.  Turn execution is one implementation: the
durable session loop, turn persistence, the mid-turn input drain, and one
serialized worker per conversation.  There is no parent, no completion, no
kind branching, and no ephemeral delta channel here; every visible reply,
activity, and event is a durable record that the timeline stream reads.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from thesis.agent.harness_tools import HarnessToolError
from thesis.agent.messages import (
    MessageContent,
    SessionMessage,
    decode_transcript,
    from_record,
)
from thesis.agent.runtime.activity import (
    ModelOutput,
    RuntimeTool,
    activity_data,
    decode_activity,
    owns_anchor,
)
from thesis.agent.runtime.child import (
    ChildConversation,
    ConversationalPolicy,
    OneShotPolicy,
)
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.control import AwaitResult
from thesis.agent.runtime.datum import Datum, tool_result_payload
from thesis.agent.runtime.delivery import (
    ROOT_ANCHOR_ID,
    Anchor,
    DeliveryError,
    DeliveryReceipt,
    DeliveryService,
    NodeInputNotAccepted,
    NodeInputNotFound,
    NodeInputReceipt,
    find_delivered,
)
from thesis.agent.runtime.inbox import SessionInbox
from thesis.agent.runtime.incoming import write_incoming
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import AnsweringActor, OperatorPolicyRegistry
from thesis.agent.runtime.session import (
    AGENT_SYSTEM_PROMPT,
    AgentSession,
    SessionPersistenceError,
    SessionResult,
    SessionToolRegistry,
    SessionTurn,
    ToolExecution,
)
from thesis.agent.runtime.spawn import SubAgentSpawner
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore, SubAgentStore
from thesis.agent.runtime.turns import NodeTurnCoordinator, is_external_input
from thesis.chat import (
    RECORD_ANCHOR_ROLES,
    ChatMessage,
    CompletionMode,
    MessageRole,
    RecordNotFound,
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
    TranscriptItemKind,
)
from thesis.chat.responder import ToolResultChunk, ToolResultObserver
from thesis.chat.service import ChatService
from thesis.providers.session_adapter import NativeProvider, NativeSessionAdapter

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "Conversation",
    "Conversations",
    "NodeTurnError",
]

#: Builds the merged, policy-scoped tool registry for one child context.
RegistryFactory = Callable[[ToolContext], SessionToolRegistry]

#: Durable statuses a conversation never leaves.
_TERMINAL = frozenset(
    {SubAgentStatus.COMPLETED, SubAgentStatus.FAILED, SubAgentStatus.CANCELLED}
)


class NodeTurnError(RuntimeError):
    """Raised when a node turn cannot be run for the requested node."""


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
    phase_executor: bool = False,
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
        phase_executor=phase_executor,
    )


def _child_system_prompt(child: SubAgentRecord) -> str:
    return (
        f"You are the '{child.role}' sub-agent. Complete the assigned task "
        "using the tools available to you. When the task is complete, "
        "respond with a concise final answer."
    )


def _tool_result_data(chunk: ToolResultChunk) -> dict[str, object]:
    """Return the canonical UI payload for one streamed tool outcome."""
    return tool_result_payload(
        call_id=chunk.call_id,
        ok=chunk.ok,
        failure=chunk.failure,
        structured=chunk.structured,
        summary=chunk.summary,
        text=chunk.text,
        tool=chunk.tool,
        datum_type=chunk.datum_type,
    )


class Conversation:
    """The only owner of one anchored transcript and its turns."""

    def __init__(self, service: Conversations, anchor: Anchor) -> None:
        self._service = service
        self._anchor = anchor

    @property
    def conversation_id(self) -> str:
        """Return the conversation's single identity: its anchor id."""
        return self._anchor.sub_agent_id or self._anchor.chat_id

    @property
    def workspace_id(self) -> str:
        """Return the workspace the conversation lives in."""
        return self._anchor.workspace_id

    @property
    def chat_id(self) -> str:
        """Return the chat that contains and stores the conversation."""
        return self._anchor.chat_id

    @property
    def kind(self) -> str:
        """Return the durable kind of the conversation's record, when present."""
        record = self._record()
        return "root" if record is None else record.kind.value

    # -- operations: each persists then wakes --------------------------------

    def deliver_session_message(
        self,
        message: SessionMessage,
        *,
        actor_id: str = "thesis-runtime",
        body: str | None = None,
        role: MessageRole | None = None,
        kind: TranscriptItemKind | None = None,
        data: Mapping[str, object] | None = None,
        tool_result: Mapping[str, object] | None = None,
        datums: Sequence[Datum] = (),
        execution_id: str | None = None,
        attachment_ids: tuple[str, ...] = (),
        investigation_ids: tuple[str, ...] = (),
        intent_epoch: int = 0,
        idempotency_key: str | None = None,
        schedule: bool = True,
    ) -> SessionMessage:
        """Deliver at this handle's anchor through the canonical service port."""
        return self._service.deliver_session_message(
            self._anchor,
            message,
            actor_id=actor_id,
            body=body,
            role=role,
            kind=kind,
            data=data,
            tool_result=tool_result,
            datums=datums,
            execution_id=execution_id,
            attachment_ids=attachment_ids,
            investigation_ids=investigation_ids,
            intent_epoch=intent_epoch,
            idempotency_key=idempotency_key,
            schedule=schedule,
        )

    def send(
        self,
        body: str,
        actor_id: str,
        *,
        role: MessageRole = MessageRole.USER,
        attachment_ids: Sequence[str] = (),
        investigation_ids: Sequence[str] = (),
        execution_id: str | None = None,
        intent_epoch: int = 0,
        schedule: bool = True,
    ) -> ChatMessage:
        """Persist one message, optionally deferring a root wake until intake ends."""
        if self._anchor.sub_agent_id is not None:
            if not schedule:
                raise DeliveryError("only root input may defer scheduling")
            if role is not MessageRole.USER or attachment_ids or investigation_ids:
                raise DeliveryError("nested input must be a plain user answer")
            return self._service.answer(
                self.workspace_id,
                self.chat_id,
                self._anchor.sub_agent_id,
                body=body,
                actor=AnsweringActor(actor_id),
            ).message
        canonical = self.deliver_session_message(
            SessionMessage(
                f"message-{uuid.uuid4().hex}", "incoming", MessageContent(body, "user")
            ),
            actor_id=actor_id,
            body=body,
            role=role,
            attachment_ids=tuple(attachment_ids),
            investigation_ids=tuple(investigation_ids),
            execution_id=execution_id,
            intent_epoch=intent_epoch,
            schedule=schedule,
        )
        return self._service.chat.get_message(self.workspace_id, canonical.message_id)

    def deliver(
        self,
        datum: Datum,
        *,
        body: str | None = None,
        kind: TranscriptItemKind = TranscriptItemKind.SUB_AGENT,
        role: MessageRole = MessageRole.TOOL,
        data: Mapping[str, object] | None = None,
        execution_id: str | None = None,
        actor_id: str = "thesis-runtime",
        idempotency_key: str | None = None,
        tool_result: Mapping[str, object] | None = None,
        schedule: bool = True,
    ) -> DeliveryReceipt:
        """Persist one datum at this anchor, then wake this conversation."""
        return self._service.deliver(
            self._anchor,
            datum,
            body=body,
            kind=kind,
            role=role,
            data=data,
            execution_id=execution_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            tool_result=tool_result,
            schedule=schedule,
        )

    def request_input(self) -> SubAgentRecord:
        """Open this conversation's input state so an answer may resume it.

        The pause is a lifecycle transition on the node's durable record: the
        question's state lives on the asking node, and the answer admitted
        through :meth:`Conversations.answer` consumes the input and returns the
        node to ``running``.
        """
        if self._anchor.sub_agent_id is None:
            raise NodeTurnError("a pause targets a nested conversation")
        return self._service.lifecycle.request_input(
            self._anchor.workspace_id, self._anchor.sub_agent_id
        )

    # -- observation ----------------------------------------------------------

    def status(self) -> str:
        """Return the conversation's durable status."""
        record = self._record()
        if record is None:
            thread = self._service.chat.get_chat(
                self._anchor.workspace_id, self._anchor.chat_id
            )
            return thread.state.value
        return record.status.value

    def in_flight(self) -> bool:
        """Return whether a turn is currently running for this conversation."""
        return self._service.in_flight(
            self._anchor.workspace_id,
            self._anchor.chat_id,
            self._anchor.sub_agent_id or ROOT_ANCHOR_ID,
        )

    def transcript(self) -> tuple[ChatMessage, ...]:
        """Return this conversation's own anchored messages, in order."""
        return self._service.transcript(self._anchor)

    # -- turn execution ------------------------------------------------------

    def run_turn(self) -> ChatMessage | SessionResult | None:
        """Run this conversation's next turn and return its outcome.

        The worker calls this; it may also be called directly to run a turn
        synchronously.  The root and every nested conversation run the same
        durable session turn over their own anchored transcript.
        """
        anchor = self._anchor.sub_agent_id or "root"
        _LOGGER.info("turn.start anchor=%s", anchor)
        started = time.monotonic()
        try:
            result = self._service.run_node_turn(
                self._anchor.workspace_id,
                self._anchor.chat_id,
                self._anchor.sub_agent_id or ROOT_ANCHOR_ID,
            )
            if not self._anchor.is_root:
                return result
            if result.status != "completed":
                return None
            if not result.turns or result.turns[-1].message.direction != "outgoing":
                return None
            final_invocation_id = result.turns[-1].invocation_id
            return next(
                (
                    message
                    for message in reversed(self.transcript())
                    if message.role is MessageRole.ASSISTANT
                    and isinstance(decode_activity(message), ModelOutput)
                    and getattr(decode_activity(message), "invocation_id", None)
                    == final_invocation_id
                ),
                None,
            )
        finally:
            _LOGGER.info(
                "turn.end anchor=%s duration_ms=%d",
                anchor,
                int((time.monotonic() - started) * 1000),
            )

    def _record(self) -> SubAgentRecord | None:
        if self._anchor.sub_agent_id is None:
            return None
        return self._service.store.get_sub_agent(
            self._anchor.workspace_id, self._anchor.sub_agent_id
        )


class _ServiceTurnRunner:
    """Adapt the service's turn dispatch to the worker's runner seam."""

    def __init__(self, service: Conversations) -> None:
        self._service = service

    def run(
        self,
        workspace_id: str,
        chat_id: str,
        sub_agent_id: str,
        *,
        pull_new_input: bool = False,
    ) -> object:
        if not sub_agent_id:
            return self._service.root(workspace_id, chat_id).run_turn()
        conversation = self._service.get(workspace_id, sub_agent_id)
        if conversation is None:
            raise NodeTurnError(f"sub-agent was not found: {sub_agent_id}")
        return conversation.run_turn()


class Conversations:
    """The only upstream surface for conversations.

    ``root`` returns the conversation at a chat's root anchor; ``get``
    resolves a conversation id; ``children`` lists one conversation's direct
    children; ``deliver`` persists a datum at an anchor and routes its wake.
    The service owns the per-conversation workers (serialize, coalesce,
    mid-turn drain), the routing rule — the root routes to itself, a record
    anchor stays durable-only, every other conversation runs on its worker —
    and spawning.
    """

    def __init__(
        self,
        *,
        chat: ChatService,
        store: SubAgentStore | None = None,
        provider: NativeProvider | None = None,
        registry_factory: RegistryFactory | None = None,
        lifecycle: SubAgentLifecycle | None = None,
        policies: OperatorPolicyRegistry | None = None,
        on_fault: Callable[[SubAgentRecord, Exception], None] | None = None,
        tool_result_observer: ToolResultObserver | None = None,
        root_actor_id: str = "thesis-agent",
        root_allowed_tools: Sequence[str] = (),
        root_system_prompt: str = AGENT_SYSTEM_PROMPT,
        max_history: int = 40,
        spawn_thread: Callable[[Callable[[], None]], None] | None = None,
        on_turn_error: Callable[[tuple[str, str, Exception]], None] | None = None,
        await_timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.25,
        await_sleep: Callable[[float], None] | None = None,
        await_monotonic: Callable[[], float] | None = None,
        read_consumed_input: Callable[[str, str, str], int] | None = None,
        acknowledge_input: Callable[[str, str, str, int], None] | None = None,
    ) -> None:
        self._chat = chat
        self._store = (
            store if store is not None else PersistenceSubAgentStore(chat.persistence)
        )
        self._lifecycle = (
            lifecycle
            if lifecycle is not None
            else SubAgentLifecycle(chat, store=self._store)
        )
        self._delivery = DeliveryService(
            chat,
            schedule_input=self.schedule_input,
            deliver_session_message=self.deliver_session_message,
        )
        self._operator_policies = (
            policies if policies is not None else OperatorPolicyRegistry()
        )
        self._spawner = SubAgentSpawner(
            lifecycle=self._lifecycle, delivery=self._delivery
        )
        self._provider = (
            NativeSessionAdapter(provider) if provider is not None else None
        )
        self._registry_factory = registry_factory
        self._on_fault = on_fault
        self.tool_result_observer = tool_result_observer
        self._root_actor_id = root_actor_id
        self._root_allowed_tools = tuple(root_allowed_tools)
        self._root_system_prompt = root_system_prompt
        if max_history < 1:
            raise ValueError("max_history must be positive")
        self._max_history = max_history
        self._await_timeout = await_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._await_sleep = await_sleep or time.sleep
        self._await_monotonic = await_monotonic or time.monotonic
        self._max_tool_iterations = 64
        self._workers = NodeTurnCoordinator(
            runner=_ServiceTurnRunner(self),
            chat=chat,
            store=self._store,
            spawn_thread=spawn_thread,
            on_error=on_turn_error,
            read_consumed_input=chat.read_consumed_input
            if read_consumed_input is None and acknowledge_input is None
            else read_consumed_input,
            acknowledge_input=chat.acknowledge_input
            if read_consumed_input is None and acknowledge_input is None
            else acknowledge_input,
        )

    @property
    def chat(self) -> ChatService:
        """Return the chat service that stores every conversation's rows."""
        return self._chat

    @property
    def store(self) -> SubAgentStore:
        """Return the durable store for nested conversation records."""
        return self._store

    @property
    def lifecycle(self) -> SubAgentLifecycle:
        """Return the lifecycle that governs nested conversation records."""
        return self._lifecycle

    @property
    def delivery(self) -> DeliveryService:
        """Return the one delivery path this service routes."""
        return self._delivery

    @property
    def max_history(self) -> int:
        """Return the reply history window for a root conversation turn."""
        return self._max_history

    @property
    def root_actor_id(self) -> str:
        """Return the actor that persists a root conversation's reply turns."""
        return self._root_actor_id

    # -- the upstream surface -------------------------------------------------

    def root(self, workspace_id: str, chat_id: str) -> Conversation:
        """Return the conversation at the chat's root anchor."""
        return Conversation(self, Anchor(workspace_id, chat_id))

    def get(
        self, workspace_id: str, conversation_id: str
    ) -> Conversation | ChildConversation | None:
        """Resolve one conversation id: a nested child, or a chat's root."""
        record = self._store.get_sub_agent(workspace_id, conversation_id)
        if record is not None:
            inner = Conversation(
                self,
                Anchor(workspace_id, record.chat_id, sub_agent_id=record.sub_agent_id),
            )
            return ChildConversation(
                self,
                inner,
                Anchor(
                    workspace_id,
                    record.chat_id,
                    sub_agent_id=record.returns_to_sub_agent_id,
                ),
                OneShotPolicy(
                    self,
                    Anchor(
                        workspace_id, record.chat_id, record.returns_to_sub_agent_id
                    ),
                )
                if record.completion_mode is CompletionMode.COMPLETE_ON_TEXT
                else ConversationalPolicy(),
            )
        try:
            thread = self._chat.get_chat(workspace_id, conversation_id)
        except RecordNotFound:
            return None
        return Conversation(self, Anchor(workspace_id, thread.chat_id))

    def spawn(
        self,
        returns_to: Anchor,
        spec: SubAgentSpec,
        prompt: str,
        kind: SubAgentKind,
        *,
        actor_id: str = "thesis-runtime",
        spawned_by_invocation_id: str | None = None,
        sub_agent_id: str | None = None,
        prompt_key: str | None = None,
        prompt_data: Mapping[str, object] | None = None,
        attribution: Mapping[str, str | None] | None = None,
        completion_mode: CompletionMode | None = None,
        phase_executor: bool = False,
    ) -> ChildConversation:
        """Create one child and give it its first prompt in a single act.

        Spawning is prompting: the durable record is created and the first
        prompt is delivered at the child's own anchor, and that delivery is
        what wakes the child's own turn.  ``returns_to`` anchors the child's
        result — its parent — and ``kind`` selects the child's turn-end
        policy, so a one-shot child completes on its final answer while a
        conversational one stays open.  ``attribution`` is caller-owned inert
        data recorded on the child's durable record.
        """
        record_fields = dict(attribution or {})
        existing = (
            self._store.get_sub_agent(returns_to.workspace_id, sub_agent_id)
            if sub_agent_id is not None
            else None
        )
        if existing is not None and existing.status not in _TERMINAL:
            # A live conversation keeps its record exactly as it is: re-prompting
            # only delivers, and the idempotent delivery is the wake.
            self._spawner.deliver_prompt(
                returns_to.workspace_id,
                returns_to.chat_id,
                existing,
                prompt,
                key=prompt_key,
                data=prompt_data,
            )
            record = existing
        else:
            spawned = self._spawner.spawn(
                returns_to.workspace_id,
                returns_to.chat_id,
                spec=spec,
                actor_id=actor_id,
                prompt=prompt,
                parent_sub_agent_id=returns_to.sub_agent_id,
                spawned_by_invocation_id=spawned_by_invocation_id,
                sub_agent_id=sub_agent_id,
                prompt_key=prompt_key,
                prompt_data=prompt_data,
                completion_mode=completion_mode
                or (
                    CompletionMode.COMPLETE_ON_TEXT
                    if kind is SubAgentKind.ONE_SHOT and not phase_executor
                    else CompletionMode.EXPLICIT_COMPLETION
                ),
                returns_to_sub_agent_id=returns_to.sub_agent_id,
                phase_executor=phase_executor,
                **record_fields,
            )
            record = spawned.record
        resolved = self.get(record.workspace_id, record.sub_agent_id)
        assert isinstance(resolved, ChildConversation)
        return resolved

    def children(
        self, workspace_id: str, conversation_id: str
    ) -> tuple[SubAgentRecord, ...]:
        """Return the direct children of one conversation."""
        record = self._store.get_sub_agent(workspace_id, conversation_id)
        if record is None:
            self._chat.get_chat(workspace_id, conversation_id)
        parent = None if record is None else record.sub_agent_id
        chat_id = conversation_id if record is None else record.chat_id
        return tuple(
            child
            for child in self._lifecycle.children(workspace_id, parent)
            if child.chat_id == chat_id
        )

    def deliver_session_message(
        self,
        anchor: Anchor,
        message: SessionMessage,
        *,
        actor_id: str = "thesis-runtime",
        body: str | None = None,
        role: MessageRole | None = None,
        kind: TranscriptItemKind | None = None,
        data: Mapping[str, object] | None = None,
        tool_result: Mapping[str, object] | None = None,
        datums: Sequence[Datum] = (),
        execution_id: str | None = None,
        attachment_ids: tuple[str, ...] = (),
        investigation_ids: tuple[str, ...] = (),
        intent_epoch: int = 0,
        idempotency_key: str | None = None,
        schedule: bool = True,
    ) -> SessionMessage:
        """Persist one neutral incoming identity before visibility or wake.

        Returns the canonical durable value, including its assigned input position.
        Presentation fields never change the neutral direction or correlation.
        """
        if message.direction != "incoming":
            raise DeliveryError("session delivery requires an incoming message")
        try:
            existing = self._chat.get_message(anchor.workspace_id, message.message_id)
        except RecordNotFound:
            existing = None
        if existing is not None:
            if existing.chat_id != anchor.chat_id or not owns_anchor(
                existing, anchor.sub_agent_id
            ):
                raise DeliveryError("message identity belongs to another conversation")
            durable = from_record(existing)
            if durable.direction != "incoming" or durable.content != message.content:
                raise DeliveryError("message identity already has different content")
            return durable
        if idempotency_key is not None:
            existing = find_delivered(self._chat, anchor, idempotency_key)
            if existing is not None:
                if existing.message_id != message.message_id:
                    raise DeliveryError(
                        "idempotency key already has a different message identity"
                    )
                return from_record(existing)
        payload = dict(data or {})
        if datums:
            payload["datums"] = [datum.as_dict() for datum in datums]
        if idempotency_key is not None:
            payload["deliveryKey"] = idempotency_key
        content = message.content
        result = content.result
        if result is not None:
            detached = content.as_dict()["result"]
            if isinstance(detached, Mapping):
                for name in (
                    "datums",
                    "invocationId",
                    "startedAt",
                    "durationMs",
                    "arguments",
                ):
                    if name in detached:
                        payload[name] = detached[name]
            correlation = content.correlation
            if correlation is not None:
                payload["toolCallId"] = correlation.request_id
                payload["tool"] = correlation.tool_name
            if tool_result is None:
                primary_type = content.source
                raw_datums = (
                    detached.get("datums") if isinstance(detached, Mapping) else None
                )
                if (
                    isinstance(raw_datums, Sequence)
                    and raw_datums
                    and isinstance(raw_datums[0], Mapping)
                ):
                    candidate = raw_datums[0].get("datumType")
                    if isinstance(candidate, str) and candidate.strip():
                        primary_type = candidate
                tool_result = tool_result_payload(
                    call_id=correlation.request_id if correlation else None,
                    tool=correlation.tool_name if correlation else None,
                    datum_type=primary_type,
                    ok=bool(result.get("ok")),
                    structured=detached.get("structured", {})
                    if isinstance(detached, Mapping)
                    and isinstance(detached.get("structured"), Mapping)
                    else {},
                    text=content.text,
                    summary=content.text
                    or f"{correlation.tool_name if correlation else content.source} result",
                    failure=detached.get("failure")
                    if isinstance(detached, Mapping)
                    and isinstance(detached.get("failure"), Mapping)
                    else None,
                )
            if body is None and isinstance(tool_result, Mapping):
                summary = tool_result.get("summary")
                text = tool_result.get("text")
                if isinstance(summary, str) and summary.strip():
                    body = summary
                elif isinstance(text, str) and text.strip():
                    body = text
        record = write_incoming(
            self._chat,
            anchor,
            message,
            actor_id=actor_id,
            body=body,
            role=role
            if role is not None
            else MessageRole.USER
            if content.source == "user"
            else MessageRole.TOOL,
            kind=kind
            if kind is not None
            else TranscriptItemKind.TOOL
            if anchor.is_root
            else TranscriptItemKind.SUB_AGENT,
            data=payload,
            tool_result=tool_result,
            execution_id=execution_id,
            attachment_ids=attachment_ids,
            investigation_ids=investigation_ids,
            intent_epoch=intent_epoch,
        )
        durable = from_record(record)
        if schedule:
            self.schedule_input(record)
        return durable

    def deliver(
        self,
        anchor: Anchor,
        datum: Datum | None,
        *,
        body: str | None = None,
        kind: TranscriptItemKind = TranscriptItemKind.SUB_AGENT,
        role: MessageRole = MessageRole.TOOL,
        data: Mapping[str, object] | None = None,
        execution_id: str | None = None,
        actor_id: str = "thesis-runtime",
        idempotency_key: str | None = None,
        tool_result: Mapping[str, object] | None = None,
        schedule: bool = True,
    ) -> DeliveryReceipt:
        """Persist one datum at an anchor, then route that anchor's wake.

        The transport generalizes beyond datum payloads (REQ-A1): a delivery
        without a datum persists a plain message built from ``body``/``role``/
        ``kind``/``data`` and requires an explicit ``idempotency_key``.
        """
        return self._delivery.deliver(
            anchor,
            datum,
            body=body,
            kind=kind,
            role=role,
            data=data,
            execution_id=execution_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            tool_result=tool_result,
            schedule=schedule,
        )

    def answer(
        self,
        workspace_id: str,
        chat_id: str,
        sub_agent_id: str,
        *,
        body: str,
        actor: AnsweringActor,
        idempotency_key: str | None = None,
    ) -> NodeInputReceipt:
        """Authorize, persist, and schedule one external answer for a node.

        This is the admission layer above the one intake (REQ-A4): admission
        control runs before any persistence -- an unknown node fails closed,
        a node not accepting unsolicited input fails closed, and the operator
        policy gate must allow the answering actor.  The node is then resumed
        (``needs_input`` -> ``running``) and the intake is performed through
        the generalized transport, carrying the same provenance payload the
        node-input path persists, so messages render identically.  The
        answerer's identity and kind are built by the trusted caller, never
        by a model or a request body.
        """
        message_body = body.strip()
        if not message_body:
            raise DeliveryError("body must be non-empty text")
        record = self._store.get_sub_agent(workspace_id, sub_agent_id)
        if record is None or record.chat_id != chat_id:
            raise NodeInputNotFound(f"sub-agent was not found: {sub_agent_id}")
        if not self._lifecycle.accepts_unsolicited_input(record):
            raise NodeInputNotAccepted(
                f"sub-agent {sub_agent_id} is not accepting input"
            )
        decision = self._operator_policies.authorize_node_input(record, actor)
        decision.require()
        _LOGGER.info(
            "operator.input.answer node=%s actor=%s",
            sub_agent_id,
            actor.actor_id,
        )
        key = (
            f"input-{uuid.uuid4().hex}" if idempotency_key is None else idempotency_key
        )
        if record.status is SubAgentStatus.NEEDS_INPUT:
            self._lifecycle.resume(workspace_id, sub_agent_id)
        receipt = self._delivery.deliver(
            Anchor(workspace_id, chat_id, sub_agent_id),
            None,
            body=message_body,
            role=MessageRole.USER,
            kind=TranscriptItemKind.SUB_AGENT,
            data={
                "subAgentId": sub_agent_id,
                "parentSubAgentId": record.parent_sub_agent_id,
                "kind": "operator_input",
                "actorKind": actor.kind.value,
                "operatorPolicy": decision.as_dict(),
            },
            actor_id=actor.actor_id,
            idempotency_key=key,
            execution_id=record.workflow_execution_id,
        )
        return NodeInputReceipt(
            message=receipt.message,
            decision=decision,
            scheduled=receipt.scheduled,
            duplicate=receipt.duplicate,
        )

    # -- scheduling: the routing rule ----------------------------------------

    def schedule_input(self, message: ChatMessage) -> None:
        """Route an accepted identity and durable ordered input position directly."""
        if not is_external_input(message):
            return
        self.schedule(
            message.workspace_id,
            message.chat_id,
            decode_activity(message).sub_agent_id or ROOT_ANCHOR_ID,
            input_id=message.message_id,
            input_position=message.input_position,
        )

    def schedule(
        self,
        workspace_id: str,
        chat_id: str,
        schedule_id: str,
        *,
        input_id: str | None = None,
        input_position: int = 0,
    ) -> None:
        """Route one wake to its conversation's worker.

        The root anchor routes to the root conversation itself; a record
        anchor is durable-only and schedules nothing, because the reacting
        party already ran; every other anchor runs on its conversation's
        worker.
        """
        if schedule_id != ROOT_ANCHOR_ID:
            record = self._store.get_sub_agent(workspace_id, schedule_id)
            if record is not None and record.status in _TERMINAL:
                return
            if record is not None and record.role in RECORD_ANCHOR_ROLES:
                _LOGGER.info(
                    "delivery.skip record anchor=%s role=%s", schedule_id, record.role
                )
                return
        if self._provider is None:
            return
        _LOGGER.info("delivery.schedule anchor=%s chat=%s", schedule_id, chat_id)
        self._workers.schedule(
            workspace_id,
            chat_id,
            schedule_id,
            input_id=input_id,
            input_position=input_position,
        )

    def in_flight(self, workspace_id: str, chat_id: str, schedule_id: str) -> bool:
        """Return whether a turn is currently running at one anchor."""
        return self._workers.in_flight(workspace_id, chat_id, schedule_id)

    def wake(self, anchor: Anchor) -> None:
        """Route one anchored conversation's wake through the routing rule."""
        self.schedule(
            anchor.workspace_id, anchor.chat_id, anchor.sub_agent_id or ROOT_ANCHOR_ID
        )

    # -- internals used by the conversation handles ---------------------------

    def transcript(self, anchor: Anchor) -> tuple[ChatMessage, ...]:
        """Return the anchored messages of one conversation, in order."""
        messages = [
            message
            for message in self._chat.list_conversation_messages(
                anchor.workspace_id,
                anchor.chat_id,
                anchor.sub_agent_id or anchor.chat_id,
            )
            if owns_anchor(message, anchor.sub_agent_id)
        ]
        start = 0
        while start < len(messages):
            if not is_external_input(messages[start]):
                start += 1
                continue
            end = start + 1
            while end < len(messages) and is_external_input(messages[end]):
                end += 1
            # Position orders incoming peers, never moves evidence across outputs.
            messages[start:end] = sorted(
                messages[start:end], key=lambda message: message.input_position
            )
            start = end
        return tuple(messages)

    def run_node_turn(
        self, workspace_id: str, chat_id: str, sub_agent_id: str
    ) -> SessionResult:
        """Run the same durable session and writer for any conversation anchor."""
        if self._provider is None:
            raise NodeTurnError("no session provider was bound")
        root = sub_agent_id == ROOT_ANCHOR_ID
        record = None if root else self._store.get_sub_agent(workspace_id, sub_agent_id)
        if not root and (record is None or record.chat_id != chat_id):
            raise NodeTurnError(f"sub-agent was not found: {sub_agent_id}")
        if record is not None and record.status in _TERMINAL:
            return SessionResult(status=record.status.value, content="", turns=())
        if record is not None and record.status is SubAgentStatus.PENDING:
            record = self._lifecycle.start(workspace_id, sub_agent_id)
        anchor = Anchor(workspace_id, chat_id, None if root else sub_agent_id)
        durable_consumed = self._chat.read_consumed_input(
            workspace_id, chat_id, sub_agent_id
        )
        read_epoch = 0 if record is None else _execution_epoch(self._chat, record)
        generation, transcript = self._workers.snapshot(
            workspace_id,
            chat_id,
            sub_agent_id,
            lambda: self.transcript(anchor),
            observe=lambda messages: [
                message.message_id for message in messages if is_external_input(message)
            ],
        )
        if not transcript:
            return SessionResult(status="completed", content="", turns=())
        selected_ids = {
            message.message_id for message in transcript[-self._max_history :]
        }
        selected_ids.update(
            message.message_id
            for message in transcript
            if is_external_input(message) and message.input_position > durable_consumed
        )
        # Keep durable arrival order so tool-history normalization can place
        # mid-call steering after its result, before later assistant turns.
        selected = [
            message for message in transcript if message.message_id in selected_ids
        ]
        included_inputs = [
            message for message in selected if is_external_input(message)
        ]
        initial_ids = tuple(message.message_id for message in included_inputs)
        seen_ids = set(initial_ids)
        initial_position = max(
            (message.input_position for message in included_inputs),
            default=durable_consumed,
        )
        consumed_position = initial_position
        boundary_generation = generation
        trigger_message_id = next(
            (
                message.message_id
                for message in reversed(transcript)
                if message.role is MessageRole.USER
            ),
            None,
        )
        context = (
            ToolContext(
                workspace_id=workspace_id,
                chat_id=chat_id,
                conversation_id=chat_id,
                actor_id=self._root_actor_id,
                trigger_message_id=trigger_message_id,
                allowed_tools=self._root_allowed_tools,
            )
            if record is None
            else _node_context(
                record,
                trigger_message_id=trigger_message_id,
                intent_epoch=read_epoch,
                phase_executor=self._owns_phase(record),
            )
        )

        def interrupted(failure: Mapping[str, object]) -> None:
            if record is not None:
                if not cancelled():
                    self._record_interruption(record, failure)
                return
            detail = (
                failure.get("message") or "the model or tool catalog is unavailable"
            )
            body = f"Reply interrupted: {detail}"
            self.deliver_session_message(
                anchor,
                SessionMessage(
                    f"message-{uuid.uuid4().hex}",
                    "incoming",
                    MessageContent(body, "runtime", result={"failure": dict(failure)}),
                ),
                actor_id=context.actor_id,
                body=body,
                role=MessageRole.SYSTEM,
                data={"status": "interrupted", "failure": dict(failure)},
                schedule=False,
            )

        def cancelled() -> bool:
            try:
                self._chat.get_chat(workspace_id, chat_id)
            except RecordNotFound:
                return True
            if record is None:
                return False
            current = self._store.get_sub_agent(workspace_id, sub_agent_id)
            return current is None or current.status in (
                SubAgentStatus.CANCELLED,
                SubAgentStatus.FAILED,
            )

        try:
            registry = (
                self._registry_factory(context)
                if self._registry_factory
                else SessionToolRegistry()
            )
        except HarnessToolError as error:
            failure = error.failure.as_dict()
            interrupted(failure)
            return SessionResult(
                status="interrupted", content="", turns=(), failure=failure
            )
        session = AgentSession(
            workspace_id=workspace_id,
            chat_id=chat_id,
            conversation_id=context.conversation_id,
            provider=self._provider,
            registry=registry,
            store=self._store,
            sub_agent_id=context.sub_agent_id,
            workflow_execution_id=context.workflow_execution_id,
            phase_attempt_id=context.phase_attempt_id,
            actor_id=context.actor_id,
            role="primary",
            max_tool_iterations=self._max_tool_iterations,
            intent_epoch=context.intent_epoch,
        )

        def refresh_registry() -> tuple[SessionToolRegistry, int]:
            nonlocal context
            if record is not None:
                context = replace(context, intent_epoch=read_epoch)
            refreshed = (
                self._registry_factory(context)
                if self._registry_factory
                else SessionToolRegistry()
            )
            return refreshed, context.intent_epoch

        def pull() -> list[SessionMessage]:
            nonlocal boundary_generation, consumed_position, read_epoch
            boundary_epoch = (
                0 if record is None else _execution_epoch(self._chat, record)
            )
            generation, snapshot = self._workers.snapshot(
                workspace_id,
                chat_id,
                sub_agent_id,
                lambda: self.transcript(anchor),
                observe=lambda messages: [
                    message.message_id
                    for message in messages
                    if is_external_input(message)
                ],
            )
            inbound = [
                message
                for message in snapshot
                if is_external_input(message)
                and message.message_id not in seen_ids
                and message.input_position > consumed_position
            ]
            seen_ids.update(message.message_id for message in inbound)
            consumed_position = max(
                consumed_position,
                max((message.input_position for message in inbound), default=0),
            )
            fresh = list(decode_transcript(inbound))
            if any(message.content.source == "user" for message in fresh):
                read_epoch = boundary_epoch
            boundary_generation = generation
            return fresh

        def consumed(messages: Sequence[SessionMessage]) -> None:
            self._workers.mark_consumed(
                workspace_id,
                chat_id,
                sub_agent_id,
                boundary_generation,
                [message.message_id for message in messages],
                input_position=max(
                    (message.position for message in messages), default=0
                ),
            )

        def reconcile(messages: Sequence[SessionMessage]) -> Sequence[SessionMessage]:
            """Restore durable arrival order without expanding retained history."""
            nonlocal read_epoch
            known = {message.message_id for message in messages}
            fresh = pull()
            known.update(message.message_id for message in fresh)
            durable = decode_transcript(
                [row for row in self.transcript(anchor) if row.message_id in known]
            )
            return durable

        def incoming(message: SessionMessage) -> SessionMessage:
            durable = self.deliver_session_message(
                anchor,
                message,
                actor_id=context.actor_id,
                execution_id=context.workflow_execution_id,
                schedule=False,
            )
            seen_ids.add(durable.message_id)
            return durable

        instruction = SessionMessage(
            f"instruction-{uuid.uuid4().hex}",
            "instruction",
            MessageContent(
                self._root_system_prompt
                if record is None
                else _child_system_prompt(record),
                "instruction",
            ),
        )
        decoded = decode_transcript(selected)
        inbox = SessionInbox(
            history=(instruction, *decoded),
            persist=incoming,
            pending_ids=tuple(
                message.message_id
                for message in included_inputs
                if message.input_position > durable_consumed
            ),
        )

        def persist(turn: SessionTurn) -> None:
            self._persist_turn(context, turn)

        try:
            result = session.run(
                (),
                inbox=inbox,
                before_model_call=pull,
                on_consumed=consumed,
                on_turn=persist,
                is_cancelled=cancelled,
                admit_dispatch=lambda: self._chat.admit_conversation_dispatch(
                    workspace_id, chat_id
                ),
                refresh_registry=refresh_registry,
                reconcile=reconcile,
            )
        except Exception as error:  # noqa: BLE001 - a phase fault fails the node
            message = str(error) or type(error).__name__
            if cancelled():
                return SessionResult(status="cancelled", content="", turns=())
            if record is None:
                return SessionResult(status="failed", content=message, turns=())
            current = self._store.get_sub_agent(workspace_id, sub_agent_id)
            if current is not None and current.status in _TERMINAL:
                return SessionResult(
                    status=current.status.value, content=message, turns=()
                )
            failed = self._lifecycle.fail(
                record.workspace_id,
                record.sub_agent_id,
                result={"failure": message},
            )
            if self._on_fault is not None:
                self._on_fault(failed or record, error)
            return SessionResult(status="failed", content=message, turns=())
        if result.status == "interrupted":
            interrupted(result.failure or {})
        if result.status == "interrupted":
            return result
        if result.status == "cancelled":
            return result
        if record is not None:
            self._child_turn_ended(record, result)
        return result

    def _owns_phase(self, record: SubAgentRecord) -> bool:
        parent = (
            self._store.get_sub_agent(record.workspace_id, record.parent_sub_agent_id)
            if record.parent_sub_agent_id
            else None
        )
        return bool(
            record.phase_executor
            and record.phase
            and record.phase_attempt_id
            and record.workflow_execution_id
            and parent is not None
            and parent.role in RECORD_ANCHOR_ROLES
            and parent.chat_id == record.chat_id
            and parent.workflow_execution_id == record.workflow_execution_id
        )

    def _persist_turn(
        self,
        context: ToolContext,
        turn: SessionTurn,
        *,
        activity: str | None = None,
    ) -> tuple[ChatMessage, ...]:
        """Persist outgoing envelopes and runtime start activity, never inputs."""
        if turn.message.direction != "outgoing":
            return ()
        content = turn.message.content
        calls = tuple(
            {
                "id": request.id,
                "type": "function",
                "function": {"name": request.name, "arguments": request.arguments},
            }
            for request in content.requests
        )
        activity_call_id = (content.result or {}).get("toolCallId")
        reasoning = (content.result or {}).get("reasoning")
        root = context.sub_agent_id is None
        metadata = (
            RuntimeTool(
                context.sub_agent_id,
                "started" if activity == "started" else "finished",
                activity_call_id if isinstance(activity_call_id, str) else None,
            )
            if activity is not None
            else ModelOutput(
                context.sub_agent_id,
                turn.invocation_id,
                None,
                calls,
            )
        )
        data = {"turn": "assistant", **activity_data(metadata)}
        if activity is None:
            data["sessionMessage"] = turn.message.as_dict()
        if turn.started_at is not None:
            data["startedAt"] = turn.started_at.isoformat()
        if activity is not None:
            data["ownerActorId"] = context.actor_id
            data["workflowExecutionId"] = context.workflow_execution_id
            record = (
                None
                if root
                else self._store.get_sub_agent(
                    context.workspace_id, context.sub_agent_id or ""
                )
            )
            if record is not None and record.chat_id == context.chat_id:
                data["phase"] = record.phase
        messages: list[ChatMessage] = []

        def append(message: ChatMessage) -> None:
            messages.append(message)

        if activity == "started":
            append(
                self._chat.create_message(
                    context.workspace_id,
                    context.chat_id,
                    actor_id=context.actor_id,
                    body=f"Running {content.source}",
                    role=MessageRole.TOOL,
                    kind=TranscriptItemKind.TOOL
                    if root
                    else TranscriptItemKind.SUB_AGENT,
                    execution_id=context.workflow_execution_id,
                    data={
                        **data,
                        "tool": content.source,
                        "arguments": dict(turn.arguments or {}),
                    },
                )
            )
        elif turn.message.direction == "outgoing" and (
            content.text.strip() or content.requests
        ):
            append(
                self._chat.create_message(
                    context.workspace_id,
                    context.chat_id,
                    actor_id=context.actor_id,
                    message_id=turn.message.message_id,
                    body=content.text.strip() or "Tool calls requested",
                    reasoning=reasoning if isinstance(reasoning, str) else None,
                    role=MessageRole.ASSISTANT,
                    kind=TranscriptItemKind.MESSAGE
                    if root
                    else TranscriptItemKind.SUB_AGENT,
                    execution_id=context.workflow_execution_id,
                    data=data,
                )
            )
        return tuple(messages)

    def execute_runtime_tool(
        self,
        context: ToolContext,
        tool: str,
        arguments: Mapping[str, object],
        execute: Callable[[], ToolExecution],
    ) -> ToolExecution:
        """Record an internal subcall without inventing a provider-call envelope."""
        from datetime import UTC, datetime

        call_id = f"runtime-{uuid.uuid4().hex}"
        started_at = datetime.now(UTC)
        started = time.monotonic()
        try:
            self._persist_turn(
                context,
                SessionTurn(
                    message=SessionMessage(
                        f"activity-{uuid.uuid4().hex}",
                        "outgoing",
                        MessageContent("", tool, result={"toolCallId": call_id}),
                    ),
                    started_at=started_at,
                    arguments=arguments,
                ),
                activity="started",
            )
        except Exception as error:  # noqa: BLE001 - writes must escape dispatch recovery
            raise SessionPersistenceError(
                "could not persist runtime tool activity"
            ) from error
        if not self._chat.admit_conversation_dispatch(
            context.workspace_id, context.chat_id
        ):
            raise RecordNotFound("conversation has been deleted")
        result = execute()
        try:
            payload = json.loads(result.as_content())
            payload.update(
                datums=[datum.as_dict() for datum in result.datums],
                startedAt=started_at.isoformat(),
                durationMs=int((time.monotonic() - started) * 1000),
                arguments=dict(arguments),
            )
            self.deliver_session_message(
                Anchor(context.workspace_id, context.chat_id, context.sub_agent_id),
                SessionMessage(
                    f"message-{uuid.uuid4().hex}",
                    "incoming",
                    MessageContent(result.text, "runtime", result=payload),
                ),
                actor_id=context.actor_id,
                body=result.text or f"{tool} result",
                data={"tool": tool, "toolCallId": call_id, "toolActivity": "finished"},
                tool_result=tool_result_payload(
                    call_id=call_id,
                    tool=tool,
                    datum_type=tool,
                    ok=result.ok,
                    structured=result.structured,
                    text=result.text,
                    summary=result.text or f"{tool} result",
                    failure=result.failure,
                ),
                execution_id=context.workflow_execution_id,
                schedule=False,
            )
        except Exception as error:  # noqa: BLE001 - writes must escape dispatch recovery
            raise SessionPersistenceError(
                "could not persist runtime tool activity"
            ) from error
        return result

    def _record_interruption(
        self, record: SubAgentRecord, failure: Mapping[str, object]
    ) -> None:
        """Keep a recoverable infrastructure interruption at its own anchor."""
        current = self._store.get_sub_agent(record.workspace_id, record.sub_agent_id)
        if current is None or current.status in _TERMINAL:
            return
        self._lifecycle.request_input(record.workspace_id, record.sub_agent_id)
        detail = failure.get("message") or "the model or tool catalog is unavailable"
        body = f"Agent turn interrupted: {detail}. Send a message to retry or steer this agent."
        self.deliver_session_message(
            Anchor(record.workspace_id, record.chat_id, record.sub_agent_id),
            SessionMessage(
                f"message-{uuid.uuid4().hex}",
                "incoming",
                MessageContent(body, "runtime", result={"failure": dict(failure)}),
            ),
            actor_id="thesis-runtime",
            body=body,
            role=MessageRole.SYSTEM,
            kind=TranscriptItemKind.SUB_AGENT,
            data={
                "subAgentId": record.sub_agent_id,
                "status": "interrupted",
                "failure": dict(failure),
            },
            schedule=False,
        )

    def _child_turn_ended(self, record: SubAgentRecord, result: SessionResult) -> None:
        """Apply the child's turn-end policy after one persisted turn.

        A child spawned through this service carries its creation-time policy;
        a child spawned by another path keeps the built-in one-shot rule, so
        every wake behaves exactly as before the policy existed.
        """
        child = self.get(record.workspace_id, record.sub_agent_id)
        if isinstance(child, ChildConversation):
            child.turn_ended(result)

    def await_child_result(
        self,
        workspace_id: str,
        conversation_id: str,
        timeout_seconds: float | None = None,
    ) -> AwaitResult:
        """Wait (bounded) for one child to finish, then return its state.

        The wait is bounded: when the child is still running at the timeout,
        the current state is returned so the caller can end its turn — the
        child's result is delivered to its return anchor when it finishes.
        """
        budget = self._await_timeout if timeout_seconds is None else timeout_seconds
        deadline = self._await_monotonic() + max(
            0.0, min(float(budget), self._await_timeout)
        )
        while True:
            result = self._await_once(workspace_id, conversation_id)
            if result.terminal or self._await_monotonic() >= deadline:
                return result
            self._await_sleep(self._poll_interval)

    def _await_once(self, workspace_id: str, conversation_id: str) -> AwaitResult:
        """Return a child's current or terminal state to its caller."""
        record = self._lifecycle.get(workspace_id, conversation_id)
        if record is None:
            raise NodeTurnError("sub-agent does not exist")
        terminal = record.status.value in {"completed", "failed", "cancelled"}
        return AwaitResult(
            sub_agent_id=record.sub_agent_id,
            status=record.status.value,
            terminal=terminal,
            result=record.result,
            result_ref=record.result_ref,
        )

    def observe_tool_result(
        self, workspace_id: str, chat_id: str, chunk: ToolResultChunk
    ) -> ToolResultChunk:
        """Run the optional observer over one streamed tool outcome."""
        observer = self.tool_result_observer
        if observer is None:
            return chunk
        try:
            observed = observer(workspace_id, chat_id, None, chunk)
        except Exception:  # observer failures are non-fatal to the reply
            return chunk
        return observed if isinstance(observed, ToolResultChunk) else chunk
