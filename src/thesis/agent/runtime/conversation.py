"""One conversation: an anchored transcript and its serialized turns.

A conversation is the only thing that owns a transcript and turns.  Its
identity is one conversation id — the chat id for a conversation at the root
anchor, the sub-agent id for a nested one — plus its workspace and kind.  Its
transcript is anchored: rows in the containing chat's shared message store,
discriminated by anchor, so every conversation in a chat streams on that
chat's unified timeline.  The chat is containment and storage scope, never a
second identity.

Operations: ``send(body, actor)``, ``deliver(datum)``, and
``answer(body, actor)`` each persist then wake; ``status`` and ``in_flight``
observe; ``subscribe`` tails the chat's live frames; ``run_turn`` is the turn
body the worker calls.  Turn execution is one implementation: the durable
session loop, turn persistence, the mid-turn input drain, and one serialized
worker per conversation.  There is no parent, no completion, and no kind
branching here.

A reply's streamed deltas are a sink concern inside the turn: a conversation
at the root anchor publishes its reply through the chat's delta sink, and a
nested conversation does not.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence

from thesis.agent.runtime.child import (
    ChildConversation,
    ConversationalPolicy,
    OneShotPolicy,
    TurnEndPolicy,
)
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.control import AwaitResult
from thesis.agent.runtime.datum import Datum, tool_result_payload
from thesis.agent.runtime.delivery import (
    ROOT_ANCHOR_ID,
    Anchor,
    DeliveryReceipt,
    DeliveryService,
    NodeInputReceipt,
    NodeInputService,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import AnsweringActor, OperatorPolicyRegistry
from thesis.agent.runtime.session import (
    AgentSession,
    CompletionProvider,
    SessionResult,
    SessionToolRegistry,
    SessionTurn,
)
from thesis.agent.runtime.spawn import SubAgentSpawner
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore, SubAgentStore
from thesis.agent.runtime.turns import NodeTurnCoordinator
from thesis.chat import (
    RECORD_ANCHOR_ROLES,
    ChatMessage,
    MessageRole,
    RecordNotFound,
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
    TranscriptItemKind,
)
from thesis.chat.responder import ReplyChunk, ToolResultChunk, ToolResultObserver
from thesis.chat.service import ChatService
from thesis.chat.streaming import (
    CHAT_MESSAGE,
    PROVIDER_DELTA,
    ChatStreamFrame,
    InMemoryChatStreamBroker,
)
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ToolCall

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "ChatSubscription",
    "Conversation",
    "Conversations",
    "NodeTurnError",
    "ReplySource",
]

#: One streamed reply stream: answer text, reasoning, and tool outcomes.
ReplyChunks = Iterator[str | ReplyChunk | ToolResultChunk]

#: The reply source of a conversation at the root anchor: it receives the
#: workspace, chat, and anchored history and yields the reply's chunks.
ReplySource = Callable[[str, str, Sequence[ChatMessage]], ReplyChunks]

#: Builds the merged, policy-scoped tool registry for one child context.
RegistryFactory = Callable[[ToolContext], SessionToolRegistry]

_HISTORY_ROLES = frozenset(
    (MessageRole.USER, MessageRole.ASSISTANT, MessageRole.SYSTEM, MessageRole.TOOL)
)

#: Durable statuses a conversation never leaves.
_TERMINAL = frozenset(
    {SubAgentStatus.COMPLETED, SubAgentStatus.FAILED, SubAgentStatus.CANCELLED}
)

_CHAT_ROLES = frozenset({"user", "assistant", "system"})


class NodeTurnError(RuntimeError):
    """Raised when a node turn cannot be run for the requested node."""


def _role_name(role: object) -> str:
    value = getattr(role, "value", role)
    return value if isinstance(value, str) else str(value)


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


def _child_system_prompt(child: SubAgentRecord) -> str:
    return (
        f"You are the '{child.role}' sub-agent. Complete the assigned task "
        "using the tools available to you. When the task is complete, "
        "respond with a concise final answer."
    )


def _seed_transcript(
    record: SubAgentRecord, transcript: Sequence[ChatMessage]
) -> list[ProviderChatMessage]:
    """Build the provider-message seed from the conversation's transcript.

    Tool results replay with their assistant tool-call envelopes; deliveries
    and unattached results replay as inbound user notes, because a ``tool``
    message without its envelope is invalid for the provider.
    """
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
                messages.append(
                    ProviderChatMessage(role="user", content=body or content)
                )
            continue
        if role in _CHAT_ROLES and body:
            messages.append(ProviderChatMessage(role=role, content=body))
    return messages


def _persist_turns(
    chat: ChatService, record: SubAgentRecord, result: SessionResult
) -> None:
    """Persist the assistant and tool turns of one finished session turn."""
    for turn in result.turns:
        if turn.role == "tool":
            _persist_tool_turn(chat, record, turn)
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
            chat.create_message(
                record.workspace_id,
                record.chat_id,
                actor_id=record.created_by,
                body=body,
                role=MessageRole.ASSISTANT,
                kind=TranscriptItemKind.SUB_AGENT,
                data=data,
            )


def _persist_tool_turn(
    chat: ChatService, record: SubAgentRecord, turn: SessionTurn
) -> None:
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
    chat.create_message(
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


def finish_one_shot_on_text(
    *,
    chat: ChatService,
    store: SubAgentStore,
    lifecycle: SubAgentLifecycle | None,
    delivery: DeliveryService | None,
    record: SubAgentRecord,
    result: SessionResult,
) -> None:
    """Complete a chat-spawned one-shot child whose turn ended in text.

    A one-shot child returns its result by its final answer: the turn ends,
    the node completes with that answer, and the result is delivered to the
    caller's anchor.  Phase agents (which carry a phase) complete only
    through the completion tool, and conversation agents stay open.
    """
    del chat
    if (
        result.status != "completed"
        or record.kind is not SubAgentKind.ONE_SHOT
        or record.phase is not None
        or lifecycle is None
    ):
        return
    current = store.get_sub_agent(record.workspace_id, record.sub_agent_id)
    if current is None or current.status is not SubAgentStatus.RUNNING:
        return
    completed = lifecycle.complete(
        record.workspace_id,
        record.sub_agent_id,
        result={"summary": result.content},
    )
    if delivery is not None:
        delivery.deliver_child_result(
            Anchor(
                record.workspace_id,
                record.chat_id,
                sub_agent_id=record.parent_sub_agent_id,
            ),
            completed,
        )


def _stream_id() -> str:
    return f"stream-{uuid.uuid4().hex}"


def _sanitize(text: str) -> str:
    """Keep printable characters plus newlines/tabs; drop other control chars."""
    return "".join(
        character for character in text if ord(character) >= 32 or character in "\n\t"
    ).strip()


def _owns_anchor(message: ChatMessage, sub_agent_id: str | None) -> bool:
    """Return whether ``message`` belongs to the conversation at the anchor.

    The conversation at the root anchor owns every message with no nested
    anchor; a nested one owns only its own.  A message created without a
    ``data`` payload is root-owned, so seeding is parameterized by anchor and
    one anchor's turns never leak into another conversation.
    """
    anchor = None if message.data is None else message.data.get("subAgentId")
    return anchor == sub_agent_id


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


class _DeltaSink:
    """The chat-stream sink: publishes reply frames on the chat's timeline."""

    def __init__(
        self,
        broker: InMemoryChatStreamBroker,
        workspace_id: str,
        chat_id: str,
    ) -> None:
        self._broker = broker
        self._workspace_id = workspace_id
        self._chat_id = chat_id

    def _publish(self, event: str, data: Mapping[str, object]) -> None:
        self._broker.publish(self._workspace_id, self._chat_id, event, data)

    def started(self, stream_id: str) -> None:
        self._publish(
            PROVIDER_DELTA,
            {"messageId": stream_id, "role": "assistant", "text": "", "start": True},
        )

    def text(self, stream_id: str, text: str) -> None:
        self._publish(PROVIDER_DELTA, {"messageId": stream_id, "text": text})

    def reasoning(self, stream_id: str, text: str) -> None:
        self._publish(
            PROVIDER_DELTA, {"messageId": stream_id, "text": text, "kind": "reasoning"}
        )

    def failed(self, stream_id: str, message: str) -> None:
        self._publish(
            PROVIDER_DELTA, {"messageId": stream_id, "error": message, "done": True}
        )

    def finished(self, stream_id: str, final_message_id: str | None = None) -> None:
        data: dict[str, object] = {"messageId": stream_id, "done": True}
        if final_message_id is not None:
            data["messageIdFinal"] = final_message_id
        self._publish(PROVIDER_DELTA, data)

    def message(self, message: ChatMessage) -> None:
        self._publish(CHAT_MESSAGE, {"message": message.as_dict()})


class ChatSubscription:
    """The live frame reader for one chat's unified timeline."""

    def __init__(
        self,
        broker: InMemoryChatStreamBroker,
        workspace_id: str,
        chat_id: str,
    ) -> None:
        self._broker = broker
        self._workspace_id = workspace_id
        self._chat_id = chat_id

    def read(self, *, after_cursor: int = 0) -> tuple[ChatStreamFrame, ...]:
        """Return frames published after ``after_cursor`` in cursor order."""
        return self._broker.read(
            self._workspace_id, self._chat_id, after_cursor=after_cursor
        )

    def latest_cursor(self) -> int:
        """Return the newest published cursor for the chat, or 0."""
        return self._broker.latest_cursor(self._workspace_id, self._chat_id)


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
    ) -> ChatMessage:
        """Persist one message at this anchor and wake this conversation."""
        message = self._service.chat.create_message(
            self._anchor.workspace_id,
            self._anchor.chat_id,
            actor_id=actor_id,
            body=body,
            role=role,
            attachment_ids=tuple(attachment_ids),
            investigation_ids=tuple(investigation_ids),
            execution_id=execution_id,
            intent_epoch=intent_epoch,
        )
        self._service.wake(self._anchor)
        return message

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

    def answer(
        self,
        body: str,
        actor: AnsweringActor,
        *,
        idempotency_key: str | None = None,
    ) -> NodeInputReceipt:
        """Authorize, persist, and schedule one answer for this conversation."""
        if self._anchor.sub_agent_id is None:
            raise NodeTurnError("an answer targets a nested conversation")
        _LOGGER.info(
            "operator.input.answer anchor=%s actor=%s",
            self._anchor.sub_agent_id,
            actor.actor_id,
        )
        return self._service.node_inputs.accept(
            self._anchor.workspace_id,
            self._anchor.chat_id,
            self._anchor.sub_agent_id,
            body=body,
            actor=actor,
            idempotency_key=idempotency_key,
        )

    def request_input(self) -> SubAgentRecord:
        """Open this conversation's input state so an answer may resume it."""
        if self._anchor.sub_agent_id is None:
            raise NodeTurnError("a pause targets a nested conversation")
        return self._service.node_inputs.request_input(
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

    def subscribe(self) -> ChatSubscription:
        """Return the live frame reader for this conversation's chat timeline."""
        return ChatSubscription(
            self._service.broker,
            self._anchor.workspace_id,
            self._anchor.chat_id,
        )

    def transcript(self) -> tuple[ChatMessage, ...]:
        """Return this conversation's own anchored messages, in order."""
        return self._service.transcript(self._anchor)

    # -- turn execution ------------------------------------------------------

    def run_turn(self) -> ChatMessage | SessionResult | None:
        """Run this conversation's next turn and return its outcome.

        The worker calls this; it may also be called directly to run a turn
        synchronously.  A conversation at the root anchor runs its reply turn
        through the chat's delta sink; a nested conversation runs one durable
        session turn over its own anchored transcript.
        """
        anchor = self._anchor.sub_agent_id or "root"
        _LOGGER.info("turn.start anchor=%s", anchor)
        started = time.monotonic()
        try:
            if self._anchor.is_root:
                return self._run_reply_turn()
            return self._service.run_node_turn(
                self._anchor.workspace_id,
                self._anchor.chat_id,
                self._anchor.sub_agent_id or ROOT_ANCHOR_ID,
            )
        finally:
            _LOGGER.info(
                "turn.end anchor=%s duration_ms=%d",
                anchor,
                int((time.monotonic() - started) * 1000),
            )

    def _run_reply_turn(self) -> ChatMessage | None:
        """Stream and persist the reply for the conversation at the root anchor."""
        service = self._service
        history = tuple(
            message
            for message in service.chat.list_messages(
                self._anchor.workspace_id, self._anchor.chat_id
            )
            if message.role in _HISTORY_ROLES and _owns_anchor(message, None)
        )[-service.max_history :]
        if not history:
            return None
        stream_id = service.next_stream_id()
        sink = _DeltaSink(
            service.broker, self._anchor.workspace_id, self._anchor.chat_id
        )
        sink.started(stream_id)
        answer_chunks: list[str] = []
        reasoning_chunks: list[str] = []
        try:
            for chunk in service.reply_for(
                self._anchor.workspace_id, self._anchor.chat_id, history
            ):
                if isinstance(chunk, ToolResultChunk):
                    chunk = service.observe_tool_result(
                        self._anchor.workspace_id, self._anchor.chat_id, chunk
                    )
                    tool_message = service.chat.create_message(
                        self._anchor.workspace_id,
                        self._anchor.chat_id,
                        actor_id=service.root_actor_id,
                        body=chunk.summary or f"{chunk.tool} result",
                        role=MessageRole.TOOL,
                        tool_result=_tool_result_data(chunk),
                        kind=TranscriptItemKind.TOOL,
                    )
                    sink.message(tool_message)
                    continue
                if isinstance(chunk, ReplyChunk):
                    reply_kind = chunk.kind
                    text = chunk.text
                else:
                    reply_kind = "text"
                    text = chunk
                if not text:
                    continue
                if reply_kind == "reasoning":
                    reasoning_chunks.append(text)
                    sink.reasoning(stream_id, text)
                    continue
                answer_chunks.append(text)
                sink.text(stream_id, text)
        except Exception as error:  # fail closed at the task boundary
            sink.failed(stream_id, str(error))
            return None
        body = _sanitize("".join(answer_chunks))
        reasoning = _sanitize("".join(reasoning_chunks))
        if not body:
            sink.finished(stream_id)
            return None
        try:
            message = service.chat.create_message(
                self._anchor.workspace_id,
                self._anchor.chat_id,
                actor_id=service.root_actor_id,
                body=body,
                role=MessageRole.ASSISTANT,
                reasoning=reasoning or None,
            )
        except Exception as error:
            # A reply must never be dropped silently: surface the failure on the
            # stream instead of letting the persistence error escape.
            sink.failed(stream_id, str(error))
            return None
        sink.message(message)
        sink.finished(stream_id, message.message_id)
        return message

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
        broker: InMemoryChatStreamBroker | None = None,
        reply_for: ReplySource | None = None,
        store: SubAgentStore | None = None,
        provider: CompletionProvider | None = None,
        registry_factory: RegistryFactory | None = None,
        lifecycle: SubAgentLifecycle | None = None,
        policies: OperatorPolicyRegistry | None = None,
        on_fault: Callable[[SubAgentRecord, Exception], None] | None = None,
        tool_result_observer: ToolResultObserver | None = None,
        root_actor_id: str = "thesis-agent",
        max_history: int = 40,
        stream_id_factory: Callable[[], str] | None = None,
        spawn_thread: Callable[[Callable[[], None]], None] | None = None,
        on_turn_error: Callable[[tuple[str, str, Exception]], None] | None = None,
        await_timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.25,
        await_sleep: Callable[[float], None] | None = None,
        await_monotonic: Callable[[], float] | None = None,
    ) -> None:
        if reply_for is not None and broker is None:
            raise ValueError("a reply source requires the chat stream broker")
        self._chat = chat
        self._broker = broker if broker is not None else InMemoryChatStreamBroker()
        self._reply_for = reply_for
        self._store = (
            store if store is not None else PersistenceSubAgentStore(chat.persistence)
        )
        self._lifecycle = (
            lifecycle
            if lifecycle is not None
            else SubAgentLifecycle(chat, store=self._store)
        )
        self._delivery = DeliveryService(chat, schedule=self.schedule)
        self._node_inputs = NodeInputService(
            chat=chat,
            store=self._store,
            policies=policies,
            schedule=self.schedule,
        )
        self._spawner = SubAgentSpawner(
            lifecycle=self._lifecycle, delivery=self._delivery
        )
        self._policies: dict[tuple[str, str], TurnEndPolicy] = {}
        self._provider = provider
        self._registry_factory = registry_factory
        self._on_fault = on_fault
        self.tool_result_observer = tool_result_observer
        self._root_actor_id = root_actor_id
        self._max_history = max_history
        self._stream_id_factory = stream_id_factory or _stream_id
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
        )

    @property
    def chat(self) -> ChatService:
        """Return the chat service that stores every conversation's rows."""
        return self._chat

    @property
    def broker(self) -> InMemoryChatStreamBroker:
        """Return the broker carrying the chats' live stream frames."""
        return self._broker

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
    def node_inputs(self) -> NodeInputService:
        """Return the node-input path that accepts answers for conversations."""
        return self._node_inputs

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
                    sub_agent_id=record.parent_sub_agent_id,
                ),
                self._policies.get(
                    (workspace_id, record.sub_agent_id), ConversationalPolicy()
                ),
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
                **record_fields,
            )
            record = spawned.record
        policy = (
            OneShotPolicy(self, returns_to)
            if kind is SubAgentKind.ONE_SHOT
            else ConversationalPolicy()
        )
        self._policies[(record.workspace_id, record.sub_agent_id)] = policy
        inner = Conversation(
            self,
            Anchor(
                record.workspace_id, record.chat_id, sub_agent_id=record.sub_agent_id
            ),
        )
        return ChildConversation(self, inner, returns_to, policy)

    def children(
        self, workspace_id: str, conversation_id: str
    ) -> tuple[SubAgentRecord, ...]:
        """Return the direct children of one conversation."""
        record = self._store.get_sub_agent(workspace_id, conversation_id)
        parent = None if record is None else record.sub_agent_id
        return self._lifecycle.children(workspace_id, parent)

    def deliver(
        self,
        anchor: Anchor,
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
        """Persist one datum at an anchor, then route that anchor's wake."""
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

    # -- scheduling: the routing rule ----------------------------------------

    def schedule(self, workspace_id: str, chat_id: str, schedule_id: str) -> None:
        """Route one wake to its conversation's worker.

        The root anchor routes to the root conversation itself; a record
        anchor is durable-only and schedules nothing, because the reacting
        party already ran; every other anchor runs on its conversation's
        worker.
        """
        if schedule_id != ROOT_ANCHOR_ID:
            record = self._store.get_sub_agent(workspace_id, schedule_id)
            if record is not None and record.role in RECORD_ANCHOR_ROLES:
                _LOGGER.info(
                    "delivery.skip record anchor=%s role=%s", schedule_id, record.role
                )
                return
        _LOGGER.info("delivery.schedule anchor=%s chat=%s", schedule_id, chat_id)
        self._workers.schedule(workspace_id, chat_id, schedule_id)

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
        return tuple(
            message
            for message in self._chat.list_messages(anchor.workspace_id, anchor.chat_id)
            if message.data is not None
            and message.data.get("subAgentId") == anchor.sub_agent_id
        )

    def run_node_turn(
        self, workspace_id: str, chat_id: str, sub_agent_id: str
    ) -> SessionResult:
        """Run one nested conversation's durable session turn.

        This is the turn body the worker calls for every nested conversation:
        it starts the record if it is still pending, seeds the provider
        messages from the conversation's own anchored transcript (replaying
        tool envelopes, turning deliveries into inbound notes), runs the
        session with the mid-turn input drain, persists the resulting turns,
        and applies the child's turn-end policy.  A session fault fails the
        node and invokes the service's fault hook.
        """
        if self._provider is None or self._registry_factory is None:
            raise NodeTurnError(
                "this service runs no nested conversations: no provider was bound"
            )
        record = self._store.get_sub_agent(workspace_id, sub_agent_id)
        if record is None or record.chat_id != chat_id:
            raise NodeTurnError(f"sub-agent was not found: {sub_agent_id}")
        if record.status is SubAgentStatus.PENDING:
            record = self._lifecycle.start(workspace_id, sub_agent_id)
        transcript = self.transcript(
            Anchor(workspace_id, chat_id, sub_agent_id=sub_agent_id)
        )
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
            for message in self.transcript(
                Anchor(workspace_id, chat_id, sub_agent_id=sub_agent_id)
            ):
                if message.message_id in seen_ids:
                    continue
                seen_ids.add(message.message_id)
                body = message.body.strip()
                if _role_name(message.role) == "user" and body:
                    fresh.append(ProviderChatMessage(role="user", content=body))
            return fresh

        try:
            result = session.run(
                _seed_transcript(record, transcript),
                before_model_call=pull,
            )
        except Exception as error:  # noqa: BLE001 - a phase fault fails the node
            message = str(error) or type(error).__name__
            failed = self._lifecycle.fail(
                record.workspace_id,
                record.sub_agent_id,
                result={"failure": message},
            )
            if self._on_fault is not None:
                self._on_fault(failed or record, error)
            return SessionResult(status="failed", content=message, turns=())
        _persist_turns(self._chat, record, result)
        self._child_turn_ended(record, result)
        return result

    def _child_turn_ended(self, record: SubAgentRecord, result: SessionResult) -> None:
        """Apply the child's turn-end policy after one persisted turn.

        A child spawned through this service carries its creation-time policy;
        a child spawned by another path keeps the built-in one-shot rule, so
        every wake behaves exactly as before the policy existed.
        """
        policy = self._policies.get((record.workspace_id, record.sub_agent_id))
        if policy is not None:
            policy.turn_ended(record, result)
            return
        finish_one_shot_on_text(
            chat=self._chat,
            store=self._store,
            lifecycle=self._lifecycle,
            delivery=self._delivery,
            record=record,
            result=result,
        )

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

    def next_stream_id(self) -> str:
        """Return the stream id of the next root reply turn."""
        return self._stream_id_factory()

    def reply_for(
        self, workspace_id: str, chat_id: str, history: Sequence[ChatMessage]
    ) -> ReplyChunks:
        """Return the reply source of a root conversation's turn."""
        if self._reply_for is None:
            raise NodeTurnError(
                "this service runs no root replies: no reply source was bound"
            )
        return self._reply_for(workspace_id, chat_id, history)

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
