"""Public conversation service: anchored intake, child lifecycle and wake routing.

The service composes handles and one isolated session attempt per turn. Incoming
producers converge here before write_incoming; output persistence and continuation
policy have separate modules, without alternate inboxes or schedulers.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence

from thesis_runtime.contracts.activity import (
    decode_activity,
    owns_anchor,
)
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.datum import Datum
from thesis_runtime.contracts.messages import (
    SessionMessage,
)
from thesis_runtime.contracts.reply import ToolResultChunk, ToolResultObserver
from thesis_runtime.conversations import (
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
from thesis_runtime.conversations.admission import ordered_messages
from thesis_runtime.conversations.delivery import (
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
from thesis_runtime.conversations.execution import (
    ConversationTurn,
    NodeTurnError,
    RegistryFactory,
    ServiceTurnRunner,
)
from thesis_runtime.conversations.handle import Conversation
from thesis_runtime.conversations.incoming import presentation_fields, write_incoming
from thesis_runtime.conversations.output import execute_runtime_tool, persist_turn
from thesis_runtime.conversations.recovery import (
    continuation_message,
    recovery_notice,
    supervisor_anchor,
)
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.conversations.turns import NodeTurnCoordinator, is_external_input
from thesis_runtime.execution.worker import Work, WorkStore
from thesis_runtime.sessions.session import (
    AGENT_SYSTEM_PROMPT,
    CompletionProvider,
    SessionResult,
    SessionTurn,
)
from thesis_runtime.subagents.child import (
    ChildConversation,
    ConversationalPolicy,
    OneShotPolicy,
)
from thesis_runtime.subagents.control import AwaitResult
from thesis_runtime.subagents.lifecycle import SubAgentLifecycle
from thesis_runtime.subagents.policy import AnsweringActor, OperatorPolicyRegistry
from thesis_runtime.subagents.spawn import SubAgentSpawner
from thesis_runtime.subagents.specs import SubAgentSpec
from thesis_runtime.subagents.store import PersistenceSubAgentStore, SubAgentStore
from thesis_runtime.tools.registry import (
    ToolExecution,
)

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "Conversation",
    "Conversations",
    "NodeTurnError",
]

#: Builds the merged, policy-scoped tool registry for one child context.


#: Durable statuses a conversation never leaves.
_TERMINAL = frozenset(
    {SubAgentStatus.COMPLETED, SubAgentStatus.FAILED, SubAgentStatus.CANCELLED}
)


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
        provider: CompletionProvider | None = None,
        registry_factory: RegistryFactory | None = None,
        lifecycle: SubAgentLifecycle | None = None,
        policies: OperatorPolicyRegistry | None = None,
        on_fault: Callable[[SubAgentRecord, Exception], None] | None = None,
        tool_result_observer: ToolResultObserver | None = None,
        root_actor_id: str = "thesis-agent",
        root_allowed_tools: Sequence[str] | None = None,
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
        work_store: WorkStore | None = None,
    ) -> None:
        self._chat = chat
        self._work_store = work_store
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
        self._provider = provider
        self._registry_factory = registry_factory
        self._on_fault = on_fault
        self.tool_result_observer = tool_result_observer
        self._root_actor_id = root_actor_id
        self._root_allowed_tools = (
            None if root_allowed_tools is None else tuple(root_allowed_tools)
        )
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
            runner=ServiceTurnRunner(self),
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
        references: Mapping[str, tuple[str, ...]] | None = None,
        intent_epoch: int = 0,
        idempotency_key: str | None = None,
        schedule: bool = True,
    ) -> ChatMessage:
        """Persist one neutral incoming identity before visibility or wake.

        Returns the canonical durable value, including its assigned input position.
        Presentation fields never change the neutral direction or correlation.
        """
        if message.direction != "incoming":
            raise DeliveryError("session delivery requires an incoming message")
        if idempotency_key is not None:
            existing = find_delivered(self._chat, anchor, idempotency_key)
            if existing is not None:
                if existing.message_id != message.message_id:
                    raise DeliveryError(
                        "idempotency key already has a different message identity"
                    )
                return existing
        payload, tool_result, body = presentation_fields(
            message,
            data=data,
            datums=datums,
            idempotency_key=idempotency_key,
            tool_result=tool_result,
            body=body,
        )
        content = message.content
        # Durable admission queues work in the same commit as this message,
        # before schedule() can apply its record-only routing rule.
        runnable = self._provider is not None and (schedule or content.source == "user")
        if runnable and anchor.sub_agent_id is not None:
            receiver = self._store.get_sub_agent(
                anchor.workspace_id, anchor.sub_agent_id
            )
            runnable = receiver is None or (
                receiver.role not in RECORD_ANCHOR_ROLES
                and receiver.status not in _TERMINAL
            )
        payload["runTurn"] = runnable
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
            references=references,
            intent_epoch=intent_epoch,
        )
        if schedule:
            self.schedule_input(record)
        return record

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
        message_id: str | None = None,
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
            (message_id or f"input-{uuid.uuid4().hex}")
            if idempotency_key is None
            else idempotency_key
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
            message_id=message_id,
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
        if self._work_store is not None:
            if input_id is not None:
                # Message admission already committed this runnable reference.
                # The durable notification stream observes that transaction.
                return
            key = f"input:{input_id}" if input_id else f"wake:{uuid.uuid4().hex}"
            self._work_store.enqueue(
                Work(
                    workspace_id,
                    key,
                    f"turn:{chat_id}:{schedule_id}",
                    "conversation",
                    {
                        "chat_id": chat_id,
                        "anchor_id": schedule_id,
                        "input_position": input_position,
                    },
                )
            )
            return
        self._workers.schedule(
            workspace_id,
            chat_id,
            schedule_id,
            input_id=input_id,
            input_position=input_position,
        )

    def in_flight(self, workspace_id: str, chat_id: str, schedule_id: str) -> bool:
        """Return whether a turn is currently running at one anchor."""
        if self._work_store is not None:
            return self._work_store.active(
                workspace_id, f"turn:{chat_id}:{schedule_id}"
            )
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
        return ordered_messages(messages)

    def run_node_turn(
        self, workspace_id: str, chat_id: str, sub_agent_id: str
    ) -> SessionResult:
        """Compose one isolated attempt, then apply the child's return policy."""
        result = ConversationTurn(
            chat=self._chat,
            store=self._store,
            lifecycle=self._lifecycle,
            workers=self._workers,
            provider=self._provider,
            registry_factory=self._registry_factory,
            deliver=self.deliver_session_message,
            persist=self._persist_turn,
            continue_turn=self._continue_turn,
            on_fault=self._on_fault,
            on_complete=self._child_turn_ended,
            root_actor_id=self._root_actor_id,
            root_allowed_tools=self._root_allowed_tools,
            root_system_prompt=self._root_system_prompt,
            max_history=self._max_history,
            max_tool_iterations=self._max_tool_iterations,
        ).run(workspace_id, chat_id, sub_agent_id)
        return result

    def _persist_turn(
        self, context: ToolContext, turn: SessionTurn, *, activity: str | None = None
    ) -> tuple[ChatMessage, ...]:
        return persist_turn(self._chat, self._store, context, turn, activity=activity)

    def execute_runtime_tool(
        self,
        context: ToolContext,
        tool: str,
        arguments: Mapping[str, object],
        execute: Callable[[], ToolExecution],
    ) -> ToolExecution:
        """Record a runtime subcall through the shared activity and incoming writers."""
        return execute_runtime_tool(
            self._chat,
            self._store,
            self.deliver_session_message,
            context,
            tool,
            arguments,
            execute,
        )

    def _continue_turn(
        self, anchor: Anchor, failure: Mapping[str, object] | None = None
    ) -> None:
        history = self._chat.read_message_window(
            anchor.workspace_id,
            anchor.chat_id,
            anchor.sub_agent_id or anchor.chat_id,
            limit=32,
        )
        message, attempt = continuation_message(history, failure, now=time.time())
        self.deliver_session_message(
            anchor,
            message,
            role=MessageRole.SYSTEM,
            data={
                "status": "continuing",
                **({"failure": dict(failure)} if failure is not None else {}),
            },
        )
        if failure is not None and anchor.sub_agent_id and attempt in (1, 4, 16, 64):
            supervisor = supervisor_anchor(self._store, anchor)
            if supervisor is not None:
                self.deliver_session_message(
                    supervisor, recovery_notice(anchor, failure)
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
