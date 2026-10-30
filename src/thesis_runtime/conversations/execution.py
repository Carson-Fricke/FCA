"""One bounded session turn and its scoped input/consumption state."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from thesis_runtime.contracts.compaction import ContextPolicy, EvidenceRanker
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.errors import ToolCatalogError
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    decode_transcript,
    from_record,
    pending_job_returns,
    unreturned_requests,
)
from thesis_runtime.contracts.records import (
    RECORD_ANCHOR_ROLES,
    ChatMessage,
    CompletionMode,
    ContextCheckpoint,
    MessageRole,
    RecordNotFound,
    SubAgentRecord,
    SubAgentStatus,
    WorkflowExecutionState,
)
from thesis_runtime.conversations.admission import message_order_key, ordered_messages
from thesis_runtime.conversations.delivery import ROOT_ANCHOR_ID, Anchor
from thesis_runtime.conversations.inbox import SessionInbox
from thesis_runtime.conversations.recovery import continuation_delay
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.conversations.turns import NodeTurnCoordinator, is_external_input
from thesis_runtime.sessions.recovery import wait_for_recovery
from thesis_runtime.sessions.session import (
    AgentSession,
    CompletionProvider,
    SessionResult,
    SessionTurn,
)
from thesis_runtime.storage.ownership import OwnershipLost
from thesis_runtime.subagents.child import has_pending_child_returns
from thesis_runtime.subagents.lifecycle import SubAgentLifecycle
from thesis_runtime.subagents.store import SubAgentStore
from thesis_runtime.tools.registry import SessionToolRegistry

if TYPE_CHECKING:
    from thesis_runtime.conversations.conversation import Conversations

_LOGGER = logging.getLogger(__name__)
RegistryFactory = Callable[[ToolContext], SessionToolRegistry]
_TERMINAL = frozenset(
    {SubAgentStatus.COMPLETED, SubAgentStatus.FAILED, SubAgentStatus.CANCELLED}
)


class NodeTurnError(RuntimeError):
    """A turn cannot run at the requested conversation anchor."""


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


def _execution_closed(chat: ChatService, record: SubAgentRecord) -> bool:
    """Stop every attributed conversation when its execution is terminal."""
    if record.workflow_execution_id is None:
        return False
    try:
        execution = chat.get_execution(
            record.workspace_id, record.workflow_execution_id
        )
    except RecordNotFound:
        return True
    return execution.state not in {
        WorkflowExecutionState.QUEUED,
        WorkflowExecutionState.RUNNING,
    }


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
    completion = (
        "Complete this conversation through its advertised completion tool. "
        "A text answer alone ends a turn and does not fulfill its explicit completion contract."
        if child.completion_mode is CompletionMode.EXPLICIT_COMPLETION
        else "When the task is complete, respond with a concise final answer."
    )
    return (
        f"You are the '{child.role}' sub-agent. Complete the assigned task using "
        "the tools available to you. Helper results arrive automatically. When "
        "no useful work is ready, end your response; unreturned calls and helper "
        "results keep the turn open and resume you on arrival. Follow each tool's "
        "result contract: a returned job handle acknowledges submission and does "
        "not establish job completion. Queued jobs require no operator action. "
        "Request operator input only for a needed human decision or fact; it pauses "
        f"until the operator replies, even if jobs finish. {completion}"
    )


class ConversationTurn:
    """Own one attempt's lifecycle, input reconciliation and session callbacks."""

    def __init__(
        self,
        *,
        chat: ChatService,
        store: SubAgentStore,
        lifecycle: SubAgentLifecycle,
        workers: NodeTurnCoordinator,
        provider: CompletionProvider | None,
        registry_factory: RegistryFactory | None,
        deliver: Callable[..., ChatMessage],
        persist: Callable[[ToolContext, SessionTurn], tuple[ChatMessage, ...]],
        continue_turn: Callable[[Anchor, Mapping[str, object] | None], None],
        on_fault: Callable[[SubAgentRecord, Exception], None] | None,
        on_complete: Callable[[SubAgentRecord, SessionResult], None],
        root_actor_id: str,
        root_allowed_tools: tuple[str, ...] | None,
        root_system_prompt: str,
        context_policy: ContextPolicy,
        evidence_ranker: EvidenceRanker | None,
        max_tool_iterations: int,
    ) -> None:
        self._chat = chat
        self._store = store
        self._lifecycle = lifecycle
        self._workers = workers
        self._provider = provider
        self._registry_factory = registry_factory
        self._deliver_session_message = deliver
        self._persist_turn = persist
        self._continue_turn = continue_turn
        self._on_fault = on_fault
        self._on_complete = on_complete
        self._root_actor_id = root_actor_id
        self._root_allowed_tools = root_allowed_tools
        self._root_system_prompt = root_system_prompt
        self._context_policy = context_policy
        self._evidence_ranker = evidence_ranker
        self._max_tool_iterations = max_tool_iterations

    def run(self, workspace_id: str, chat_id: str, sub_agent_id: str) -> SessionResult:
        """Run the same durable session and writer for any conversation anchor."""
        self._workspace_id = workspace_id
        self._chat_id = chat_id
        self._sub_agent_id = sub_agent_id
        if self._provider is None:
            raise NodeTurnError("no session provider was bound")
        root = self._sub_agent_id == ROOT_ANCHOR_ID
        self._record = (
            None
            if root
            else self._store.get_sub_agent(self._workspace_id, self._sub_agent_id)
        )
        if not root and (self._record is None or self._record.chat_id != self._chat_id):
            raise NodeTurnError(f"sub-agent was not found: {self._sub_agent_id}")
        if self._record is not None and (
            self._record.status in _TERMINAL or self._record.role in RECORD_ANCHOR_ROLES
        ):
            return SessionResult(status=self._record.status.value, content="", turns=())
        if (
            self._record is not None
            and self._record.status is SubAgentStatus.NEEDS_INPUT
        ):
            return SessionResult(status="needs_input", content="", turns=())
        if self._record is not None and _execution_closed(self._chat, self._record):
            return SessionResult(status="cancelled", content="", turns=())
        if self._record is not None and self._record.status is SubAgentStatus.PENDING:
            self._record = self._lifecycle.start(self._workspace_id, self._sub_agent_id)
        self._anchor = Anchor(
            self._workspace_id, self._chat_id, None if root else self._sub_agent_id
        )
        durable_consumed = self._chat.read_consumed_input(
            self._workspace_id, self._chat_id, self._sub_agent_id
        )
        self._read_epoch = (
            0 if self._record is None else _execution_epoch(self._chat, self._record)
        )
        self._checkpoint = self._chat.read_context_checkpoint(
            self._workspace_id, self._chat_id, self._context_id(root)
        )
        generation, transcript = self._workers.snapshot(
            self._workspace_id,
            self._chat_id,
            self._sub_agent_id,
            lambda: self._read_context(root),
            observe=lambda messages: [
                message.message_id for message in messages if is_external_input(message)
            ],
        )
        if not transcript:
            return SessionResult(status="completed", content="", turns=())
        selected = list(transcript)
        self._retained = {message.message_id: message for message in selected}
        included_inputs = [
            message for message in selected if is_external_input(message)
        ]
        initial_ids = tuple(message.message_id for message in included_inputs)
        self._seen_ids = set(initial_ids)
        initial_position = max(
            durable_consumed,
            max((message.input_position for message in included_inputs), default=0),
        )
        self._consumed_position = initial_position
        self._boundary_generation = generation
        trigger_message_id = next(
            (
                message.message_id
                for message in reversed(transcript)
                if message.role is MessageRole.USER
            ),
            None,
        )
        self._context = (
            ToolContext(
                workspace_id=self._workspace_id,
                chat_id=self._chat_id,
                conversation_id=self._chat_id,
                actor_id=self._root_actor_id,
                trigger_message_id=trigger_message_id,
                allowed_tools=self._root_allowed_tools,
            )
            if self._record is None
            else _node_context(
                self._record,
                trigger_message_id=trigger_message_id,
                intent_epoch=self._read_epoch,
                phase_executor=self._owns_phase(self._record),
            )
        )
        delay = continuation_delay(selected, time.time())
        if delay is not None:
            wait_for_recovery(delay, cancelled=self._cancelled, steered=self._steered)
        if self._cancelled():
            return SessionResult(status="cancelled", content="", turns=())
        try:
            registry = (
                self._registry_factory(self._context)
                if self._registry_factory
                else SessionToolRegistry()
            )
        except ToolCatalogError as error:
            failure = error.outcome
            self._interrupted(failure)
            return SessionResult(
                status="interrupted", content="", turns=(), failure=failure
            )
        session = AgentSession(
            workspace_id=self._workspace_id,
            chat_id=self._chat_id,
            conversation_id=self._context.conversation_id,
            tool_context=self._context,
            provider=self._provider,
            registry=registry,
            store=self._store,
            sub_agent_id=self._context.sub_agent_id,
            workflow_execution_id=self._context.workflow_execution_id,
            phase_attempt_id=self._context.phase_attempt_id,
            actor_id=self._context.actor_id,
            role="primary",
            max_tool_iterations=self._max_tool_iterations,
            intent_epoch=self._context.intent_epoch,
            timeline=self._chat.timeline,
            context_policy=self._context_policy,
            evidence_ranker=self._evidence_ranker,
        )
        system_prompt = (
            self._root_system_prompt
            if self._record is None
            else _child_system_prompt(self._record)
        )
        instruction = SessionMessage(
            f"instruction-{uuid.uuid4().hex}",
            "instruction",
            MessageContent(system_prompt, "instruction"),
        )
        decoded = decode_transcript(selected)
        inbox = SessionInbox(
            history=(instruction, *decoded),
            persist=self._incoming,
            pending_ids=tuple(
                message.message_id
                for message in included_inputs
                if message.input_position > durable_consumed
            ),
        )
        try:
            result = session.run(
                (),
                inbox=inbox,
                before_model_call=self._pull,
                on_consumed=self._consumed,
                on_turn=self._persist,
                is_cancelled=self._cancelled,
                is_steered=self._steered,
                admit_dispatch=self._admit_dispatch,
                refresh_registry=self._refresh_registry,
                reconcile=self._reconcile,
                on_checkpoint=self._save_checkpoint,
                has_pending_returns=self._has_pending_returns,
            )
        except OwnershipLost:
            raise
        except ToolCatalogError as error:
            self._interrupted(error.outcome)
            return SessionResult(
                status="interrupted", content="", turns=(), failure=error.outcome
            )
        except Exception as error:
            _LOGGER.exception("conversation turn failed: %s", self._chat_id)
            message = str(error) or type(error).__name__
            if self._cancelled():
                return SessionResult(status="cancelled", content="", turns=())
            if self._record is None:
                self._interrupted(
                    {
                        "kind": "runtime_fault",
                        "message": message,
                        "certainty": "unknown_outcome",
                        "retry_safe": False,
                    },
                    resume=False,
                )
                raise
            current = self._store.get_sub_agent(self._workspace_id, self._sub_agent_id)
            if current is not None and current.status in _TERMINAL:
                return SessionResult(
                    status=current.status.value, content=message, turns=()
                )
            failed = self._lifecycle.fail(
                self._record.workspace_id,
                self._record.sub_agent_id,
                result={"failure": message},
            )
            if self._on_fault is not None:
                self._on_fault(failed or self._record, error)
            return SessionResult(status="failed", content=message, turns=())
        if result.status == "interrupted":
            self._interrupted(
                result.failure or {},
                resume=(result.failure or {}).get("kind")
                not in {"context_capacity", "compaction_failed"},
            )
            return result
        if result.status == "yielded":
            if not self._cancelled():
                self._continue_turn(self._anchor, None)
            return result
        if result.status == "waiting":
            return result
        if result.status != "cancelled" and self._record is not None:
            self._on_complete(self._record, result)
        return result

    def _context_id(self, root: bool) -> str:
        return self._chat_id if root else self._sub_agent_id

    def _has_pending_returns(self) -> bool:
        """Read existing scoped messages, including calls predating compaction."""
        self._pull()
        messages = decode_transcript(
            ordered_messages(tuple(self._retained.values()))
            if self._checkpoint is None
            else self._chat.read_message_window(
                self._workspace_id, self._chat_id, self._context.conversation_id
            )
        )
        consumed = self._chat.read_consumed_input(
            self._workspace_id, self._chat_id, self._sub_agent_id
        )
        return (
            any(message.position > consumed for message in messages)
            or bool(unreturned_requests(messages))
            or bool(pending_job_returns(messages))
            or has_pending_child_returns(messages)
        )

    def _read_context(self, root: bool) -> tuple[ChatMessage, ...]:
        checkpoint = self._checkpoint
        rows = ordered_messages(
            self._chat.read_message_window(
                self._workspace_id,
                self._chat_id,
                self._context_id(root),
                after_message_id=None
                if checkpoint is None
                else checkpoint.through_message_id,
            )
        )
        if checkpoint is None:
            return rows
        identities = (checkpoint.summary_message_id, *checkpoint.retained_message_ids)
        prefix = tuple(
            self._chat.get_message(self._workspace_id, identity)
            for identity in identities
        )
        if any(
            (row.chat_id, row.conversation_id)
            != (self._chat_id, self._context_id(root))
            for row in prefix
        ):
            raise NodeTurnError("context checkpoint contains foreign evidence")
        return (
            *prefix,
            *(
                row
                for row in rows
                if row.message_id not in identities
                and not (
                    isinstance((row.data or {}).get("sessionMessage"), Mapping)
                    and (from_record(row).content.result or {}).get(
                        "context_compaction"
                    )
                )
            ),
        )

    def _save_checkpoint(
        self,
        through_id: str,
        summary: SessionMessage,
        replacement: Sequence[SessionMessage],
    ) -> None:
        if self._checkpoint is not None:
            previous = self._retained.get(
                self._checkpoint.through_message_id
            ) or self._chat.get_message(
                self._workspace_id, self._checkpoint.through_message_id
            )
            current = self._retained[through_id]
            if message_order_key(previous) > message_order_key(current):
                through_id = previous.message_id
        boundary = self._retained.get(through_id) or self._chat.get_message(
            self._workspace_id, through_id
        )
        boundary_key = message_order_key(boundary)
        retained_ids = tuple(
            row.message_id
            for row in replacement
            if row.direction != "instruction"
            and row.message_id != summary.message_id
            and message_order_key(self._retained[row.message_id]) <= boundary_key
        )
        checkpoint = ContextCheckpoint(
            workspace_id=self._workspace_id,
            chat_id=self._chat_id,
            conversation_id=self._context.conversation_id,
            root_conversation_id=self._chat_id,
            parent_conversation_id=None
            if self._record is None
            else self._record.parent_sub_agent_id or self._chat_id,
            created_at=datetime.now(UTC),
            created_by=self._context.actor_id,
            checkpoint_id=self._context.conversation_id,
            summary_message_id=summary.message_id,
            through_message_id=through_id,
            retained_message_ids=retained_ids,
        )
        self._chat.save_context_checkpoint(checkpoint)
        self._checkpoint = checkpoint

    def _interrupted(
        self, failure: Mapping[str, object], *, resume: bool = True
    ) -> None:
        if not self._cancelled():
            if not resume:
                self._deliver_session_message(
                    self._anchor,
                    SessionMessage(
                        f"message-{uuid.uuid4().hex}",
                        "incoming",
                        MessageContent(
                            f"Reply interrupted: {failure.get('message')}",
                            "runtime",
                            result={"failure": dict(failure)},
                        ),
                    ),
                    role=MessageRole.SYSTEM,
                    data={"status": "interrupted", "failure": dict(failure)},
                    schedule=False,
                )
            else:
                self._continue_turn(self._anchor, failure)

    def _cancelled(self) -> bool:
        try:
            self._chat.get_chat(self._workspace_id, self._chat_id)
        except RecordNotFound:
            return True
        if self._record is None:
            return False
        current = self._store.get_sub_agent(self._workspace_id, self._sub_agent_id)
        return (
            current is None
            or (
                current.status is not SubAgentStatus.COMPLETED
                and _execution_closed(self._chat, current)
            )
            or current.status in (SubAgentStatus.CANCELLED, SubAgentStatus.FAILED)
        )

    def _admit_dispatch(self) -> bool:
        if not self._chat.admit_conversation_dispatch(
            self._workspace_id, self._chat_id
        ):
            return False
        if self._record is None:
            return True
        current = self._store.get_sub_agent(self._workspace_id, self._sub_agent_id)
        return (
            current is not None
            and current.status not in _TERMINAL
            and (not _execution_closed(self._chat, current))
        )

    def _refresh_registry(self) -> tuple[SessionToolRegistry, int]:
        if self._record is not None:
            self._context = replace(self._context, intent_epoch=self._read_epoch)
        refreshed = (
            self._registry_factory(self._context)
            if self._registry_factory
            else SessionToolRegistry()
        )
        return (refreshed, self._context.intent_epoch)

    def _pull(self) -> list[SessionMessage]:
        boundary_epoch = (
            0 if self._record is None else _execution_epoch(self._chat, self._record)
        )
        generation, snapshot = self._workers.snapshot(
            self._workspace_id,
            self._chat_id,
            self._sub_agent_id,
            lambda: ordered_messages(
                self._chat.read_message_window(
                    self._workspace_id,
                    self._chat_id,
                    self._anchor.sub_agent_id or self._chat_id,
                    after_position=self._consumed_position,
                )
            ),
            observe=lambda messages: [
                message.message_id for message in messages if is_external_input(message)
            ],
        )
        self._retained.update((message.message_id, message) for message in snapshot)
        inbound = [
            message
            for message in snapshot
            if is_external_input(message)
            and message.message_id not in self._seen_ids
            and (message.input_position > self._consumed_position)
        ]
        self._seen_ids.update(message.message_id for message in inbound)
        self._consumed_position = max(
            self._consumed_position,
            max((message.input_position for message in snapshot), default=0),
        )
        fresh = list(decode_transcript(inbound))
        if any(message.content.source == "user" for message in fresh):
            self._read_epoch = boundary_epoch
        self._boundary_generation = generation
        return fresh

    def _consumed(self, messages: Sequence[SessionMessage]) -> None:
        self._workers.mark_consumed(
            self._workspace_id,
            self._chat_id,
            self._sub_agent_id,
            self._boundary_generation,
            [message.message_id for message in messages],
            input_position=max((message.position for message in messages), default=0),
        )

    def _steered(self) -> bool:
        return any(
            is_external_input(message) and from_record(message).content.source == "user"
            for message in self._chat.read_message_window(
                self._workspace_id,
                self._chat_id,
                self._anchor.sub_agent_id or self._chat_id,
                after_position=self._consumed_position,
            )
        )

    def _reconcile(
        self, messages: Sequence[SessionMessage]
    ) -> Sequence[SessionMessage]:
        """Restore durable arrival order without expanding retained history."""
        known = {message.message_id for message in messages}
        fresh = self._pull()
        known.update(message.message_id for message in fresh)
        durable = decode_transcript(
            [
                row
                for row in ordered_messages(tuple(self._retained.values()))
                if row.message_id in known
            ]
        )
        if self._checkpoint is not None:
            ids = (
                self._checkpoint.summary_message_id,
                *self._checkpoint.retained_message_ids,
            )
            by_id = {row.message_id: row for row in durable}
            durable = tuple(
                by_id[identity] for identity in ids if identity in by_id
            ) + tuple(row for row in durable if row.message_id not in ids)
        return durable

    def _incoming(self, message: SessionMessage) -> SessionMessage:
        known = self._retained.get(message.message_id)
        if known is not None and from_record(known) == message:
            return message
        durable = self._deliver_session_message(
            self._anchor,
            message,
            actor_id=self._context.actor_id,
            execution_id=self._context.workflow_execution_id,
            schedule=False,
        )
        self._seen_ids.add(durable.message_id)
        self._retained[durable.message_id] = durable
        return from_record(durable)

    def _persist(self, turn: SessionTurn) -> None:
        self._retained.update(
            (row.message_id, row) for row in self._persist_turn(self._context, turn)
        )

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
            and (parent is not None)
            and (parent.role in RECORD_ANCHOR_ROLES)
            and (parent.chat_id == record.chat_id)
            and (parent.workflow_execution_id == record.workflow_execution_id)
        )


class ServiceTurnRunner:
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
