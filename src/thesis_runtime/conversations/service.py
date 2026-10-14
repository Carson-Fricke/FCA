"""Application boundary for workspace chat and workflow lifecycle commands."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from queue import Queue
from threading import Lock, RLock, Thread
from typing import Protocol, TypeVar, cast

from thesis_runtime.contracts.activity import is_external_input
from thesis_runtime.contracts.records import (
    WORKFLOW_NODE_ROLE,
    AgentInvocation,
    Attachment,
    AttachmentState,
    ChatContractError,
    ChatMessage,
    ChatState,
    ChatThread,
    ConversationRecord,
    CrossWorkspaceLinkError,
    InvalidLifecycleTransition,
    MessageRole,
    MessageState,
    RecordLinks,
    RecordNotFound,
    SubAgentRecord,
    SubAgentStatus,
    ToolJob,
    ToolJobConflictError,
    ToolJobState,
    TranscriptItemKind,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowExecutionState,
    merge_references,
    replace_record,
)
from thesis_runtime.conversations.timeline import TimelineWriter
from thesis_runtime.execution.events import (
    ActorScope,
    EventReplay,
    ExecutionEvent,
    ExecutionEventStore,
    ExecutionEventType,
    InMemoryExecutionEventStore,
)
from thesis_runtime.storage.ownership import OwnershipStore, assert_owned
from thesis_runtime.storage.ownership import mutation_guard as storage_guard

Record = (
    ChatThread
    | ChatMessage
    | Attachment
    | WorkflowExecution
    | ToolJob
    | SubAgentRecord
    | AgentInvocation
)
RecordType = type[Record]
RecordT = TypeVar("RecordT", bound=Record)
TransitionT = TypeVar("TransitionT")


_STEERABLE_EXECUTION_STATES = frozenset(
    {
        WorkflowExecutionState.QUEUED,
        WorkflowExecutionState.RUNNING,
    }
)

_TERMINAL_SUB_AGENT_STATUSES = frozenset(
    {
        SubAgentStatus.COMPLETED,
        SubAgentStatus.FAILED,
        SubAgentStatus.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class SteeringPercolation:
    """A durably acknowledged steering message and its targeted executions."""

    message: ChatMessage
    executions: tuple[WorkflowExecution, ...]


class ChatPersistence(Protocol):
    """Storage port for immutable workspace record snapshots."""

    def save(self, record: RecordT) -> RecordT: ...

    def admit_message(self, message: ChatMessage) -> ChatMessage: ...

    def update_chat(
        self,
        workspace_id: str,
        chat_id: str,
        update: Callable[[ChatThread], ChatThread],
    ) -> ChatThread:
        """Atomically recompute a pure update against the current root."""
        ...

    def read_message_window(
        self,
        workspace_id: str,
        chat_id: str,
        conversation_id: str,
        *,
        after_position: int | None,
        consumed_position: int,
        limit: int,
    ) -> tuple[ChatMessage, ...]: ...

    def get(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> RecordT | None: ...

    def list(
        self, record_type: type[RecordT], workspace_id: str
    ) -> tuple[RecordT, ...]: ...

    def delete(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> None: ...

    def list_conversation(
        self,
        record_type: type[RecordT],
        workspace_id: str,
        root_conversation_id: str,
        *,
        conversation_id: str | None = None,
    ) -> tuple[RecordT, ...]: ...

    def mark_conversation_deleted(
        self, workspace_id: str, chat_id: str, deleted_at: datetime
    ) -> None: ...

    def purge_conversation(
        self, workspace_id: str, chat_id: str, limit: int = 100
    ) -> bool: ...

    def list_deleted_conversations(
        self, workspace_id: str
    ) -> tuple[ChatThread, ...]: ...


class InMemoryChatPersistence:
    """Small deterministic persistence adapter suitable for local runtime tests."""

    _record_types: tuple[RecordType, ...] = (
        ChatThread,
        ChatMessage,
        Attachment,
        WorkflowExecution,
        ToolJob,
        SubAgentRecord,
        AgentInvocation,
    )

    def __init__(self, records: Iterable[Record] = ()) -> None:
        self._guard = RLock()
        self._records: dict[type[Record], dict[tuple[str, str], Record]] = {
            record_type: {} for record_type in self._record_types
        }
        for record in records:
            self.save(record)

    def save(self, record: RecordT) -> RecordT:
        with self._guard:
            if isinstance(record, ConversationRecord):
                root = self.get(ChatThread, record.workspace_id, record.chat_id)
                if root is not None and root.state is ChatState.DELETED:
                    raise ChatContractError("conversation has been deleted")
            return self._save(record)

    def admit_message(self, message: ChatMessage) -> ChatMessage:
        from thesis_runtime.conversations.admission import (
            arrival_sequence,
            existing_admission,
            message_bookkeeping,
            position_message,
            require_admission_chat,
        )

        with self._guard:
            root = require_admission_chat(
                self.get(ChatThread, message.workspace_id, message.chat_id)
            )
            previous = self.get(ChatMessage, message.workspace_id, message.message_id)
            if previous is not None:
                return existing_admission(previous, message)
            rows = self.list_conversation(
                ChatMessage,
                message.workspace_id,
                message.chat_id,
                conversation_id=message.conversation_id,
            )
            all_rows = self.list_conversation(
                ChatMessage, message.workspace_id, message.chat_id
            )
            arrival = (
                max(
                    (arrival_sequence(row) for row in all_rows),
                    default=0,
                )
                + 1
            )
            admitted = position_message(
                message,
                max((row.input_position for row in rows), default=0) + 1,
                arrival_sequence=arrival,
            )
            self.save(admitted)
            self._save(message_bookkeeping(root, admitted))
            return admitted

    def update_chat(
        self,
        workspace_id: str,
        chat_id: str,
        update: Callable[[ChatThread], ChatThread],
    ) -> ChatThread:
        from thesis_runtime.conversations.admission import require_live_chat

        with self._guard:
            root = require_live_chat(self.get(ChatThread, workspace_id, chat_id))
            return self._save(update(root))

    def read_message_window(
        self,
        workspace_id: str,
        chat_id: str,
        conversation_id: str,
        *,
        after_position: int | None,
        consumed_position: int,
        limit: int,
    ) -> tuple[ChatMessage, ...]:
        from thesis_runtime.conversations.admission import select_message_window

        with self._guard:
            return select_message_window(
                self.list_conversation(
                    ChatMessage, workspace_id, chat_id, conversation_id=conversation_id
                ),
                after_position=after_position,
                consumed_position=consumed_position,
                limit=limit,
            )

    def _save(self, record: RecordT) -> RecordT:
        record_type = type(record)
        if record_type not in self._records:
            raise TypeError(f"unsupported chat record type: {record_type.__name__}")
        record_id = _record_id(record)
        self._records[record_type][(record.workspace_id, record_id)] = record
        return record

    def get(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> RecordT | None:
        value = self._records[record_type].get((workspace_id, record_id))
        return cast(RecordT | None, value)

    def list(
        self, record_type: type[RecordT], workspace_id: str
    ) -> tuple[RecordT, ...]:
        # Preserve insertion order: it is the stable tie-break callers rely on
        # when several records share one clock tick.
        values = tuple(
            record
            for (stored_workspace, _), record in self._records[record_type].items()
            if stored_workspace == workspace_id
            and not (
                isinstance(record, ChatThread) and record.state is ChatState.DELETED
            )
        )
        return cast(tuple[RecordT, ...], values)

    def list_conversation(
        self,
        record_type: type[RecordT],
        workspace_id: str,
        root_conversation_id: str,
        *,
        conversation_id: str | None = None,
    ) -> tuple[RecordT, ...]:
        return tuple(
            record
            for record in self.list(record_type, workspace_id)
            if isinstance(record, ConversationRecord)
            and record.root_conversation_id == root_conversation_id
            and (conversation_id is None or record.conversation_id == conversation_id)
        )

    def mark_conversation_deleted(
        self, workspace_id: str, chat_id: str, deleted_at: datetime
    ) -> None:
        with self._guard:
            root = self.get(ChatThread, workspace_id, chat_id)
            if root is not None and root.state is not ChatState.DELETED:
                self._save(
                    replace_record(root, state=ChatState.DELETED, updated_at=deleted_at)
                )

    def list_deleted_conversations(self, workspace_id: str) -> tuple[ChatThread, ...]:
        return tuple(
            cast(ChatThread, record)
            for (ws, _), record in self._records[ChatThread].items()
            if ws == workspace_id
            and cast(ChatThread, record).state is ChatState.DELETED
        )

    def purge_conversation(
        self, workspace_id: str, chat_id: str, limit: int = 100
    ) -> bool:
        with self._guard:
            remaining = limit
            for record_type in self._record_types:
                if record_type is ChatThread:
                    continue
                for record in self.list_conversation(
                    record_type, workspace_id, chat_id
                ):
                    if remaining == 0:
                        return False
                    self.delete(type(record), workspace_id, _record_id(record))
                    remaining -= 1
            return True

    def delete(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> None:
        self._records[record_type].pop((workspace_id, record_id), None)


def _record_id(record: Record) -> str:
    id_name = {
        ChatThread: "chat_id",
        ChatMessage: "message_id",
        Attachment: "attachment_id",
        WorkflowExecution: "execution_id",
        ToolJob: "job_id",
        SubAgentRecord: "sub_agent_id",
        AgentInvocation: "invocation_id",
    }.get(type(record))
    if id_name is not None:
        value = getattr(record, id_name)
        if isinstance(value, str):
            return value
    raise TypeError(f"record has no stable ID: {type(record).__name__}")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class ChatService:
    """Workspace-safe commands with atomic storage admission and root updates."""

    def __init__(
        self,
        persistence: ChatPersistence | None = None,
        *,
        events: ExecutionEventStore | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
        ownership: OwnershipStore | None = None,
        single_process: bool = True,
        timeline: TimelineWriter | None = None,
    ) -> None:
        if not single_process and ownership is None:
            raise ValueError("distributed chat ownership requires an ownership store")
        self.ownership = ownership
        self.timeline = timeline
        self._persistence = (
            InMemoryChatPersistence() if persistence is None else persistence
        )
        self._events = InMemoryExecutionEventStore() if events is None else events
        self._clock = clock
        self._id_factory = id_factory
        self._guard = Lock()
        self._executions: dict[tuple[str, str], RLock] = {}
        self._chat_records: dict[tuple[str, str], RLock] = {}
        self._cleanup_guard = Lock()
        self._cleanup_queue: Queue[tuple[str, str]] = Queue()
        self._cleanup_pending: set[tuple[str, str]] = set()
        self._cleanup_worker: Thread | None = None
        self._workspace_is_deleted: Callable[[str], bool] = lambda _: False

    def bind_workspace_deletion(self, is_deleted: Callable[[str], bool]) -> None:
        """Inject the application's durable workspace lifecycle read port."""
        self._workspace_is_deleted = is_deleted

    def _require_live_workspace(self, workspace_id: str) -> None:
        if self._workspace_is_deleted(workspace_id):
            raise RecordNotFound("workspace has been deleted")

    def delete_workspace_conversations(self, workspace_id: str) -> None:
        """Fence all roots, including archived chats, and resume their cleanup.

        Read the persistence port directly: the workspace lifecycle already
        hides public reads. Descendants share each root's deletion boundary.
        """
        for root in self._persistence.list(ChatThread, workspace_id):
            self.delete_chat(workspace_id, root.chat_id)
        self.resume_conversation_cleanup(workspace_id)

    def mutation_guard(
        self, workspace_id: str, resource: str
    ) -> AbstractContextManager:
        """Protect a bounded mutation across replicas, or locally in an embedder."""
        if self.ownership is not None:
            return storage_guard(self.ownership, workspace_id, resource)
        with self._guard:
            return self._chat_records.setdefault((workspace_id, resource), RLock())

    def _chat_record_guard(
        self, workspace_id: str, chat_id: str
    ) -> AbstractContextManager:
        if self.ownership is not None:
            return storage_guard(self.ownership, workspace_id, f"chat:{chat_id}")
        with self._guard:
            return self._chat_records.setdefault((workspace_id, chat_id), RLock())

    def _update_chat(
        self,
        workspace_id: str,
        chat_id: str,
        update: Callable[[ChatThread], ChatThread],
    ) -> ChatThread:
        self._require_live_workspace(workspace_id)
        return self._persistence.update_chat(workspace_id, chat_id, update)

    def execution_guard(
        self, workspace_id: str, execution_id: str
    ) -> AbstractContextManager:
        """Return the shared reentrant boundary for execution-owned mutations.

        Runtime lifecycle and workflow callers may compose storage mutations
        with acceptance under this same single-owner guard. Reread records
        after acquiring it; locking a stale snapshot does not make it current.
        Never hold it across model/tool calls, intake, scheduling, or node-lock
        acquisition. Publish events after releasing the outermost guard.
        """
        if self.ownership is not None:
            return storage_guard(
                self.ownership, workspace_id, f"execution:{execution_id}"
            )
        with self._guard:
            return self._executions.setdefault((workspace_id, execution_id), RLock())

    @property
    def persistence(self) -> ChatPersistence:
        """Return the configured persistence port for composition and inspection."""
        return self._persistence

    @property
    def event_store(self) -> ExecutionEventStore:
        """Return the configured durable event-store port."""
        return self._events

    def create_chat(
        self,
        workspace_id: str,
        *,
        title: str,
        owner_actor_id: str,
        references: Mapping[str, tuple[str, ...]] | None = None,
    ) -> ChatThread:
        """Create one chat with opaque application resource references."""
        links = RecordLinks(references=references or {})
        now = self._now()
        chat_id = self._id_factory("chat")
        chat = ChatThread(
            workspace_id=workspace_id,
            created_at=now,
            created_by=owner_actor_id,
            chat_id=chat_id,
            title=title,
            owner_actor_id=owner_actor_id,
            links=links,
            updated_at=now,
            conversation_id=chat_id,
            root_conversation_id=chat_id,
            parent_conversation_id=None,
        )
        self._save(chat)
        return chat

    def link_resources(
        self, workspace_id: str, chat_id: str, references: Mapping[str, tuple[str, ...]]
    ) -> ChatThread:
        """Attach application references without reading application-owned records."""
        now = self._now()
        return self._update_chat(
            workspace_id,
            chat_id,
            lambda current: replace_record(
                current,
                links=current.links.add(references=references),
                updated_at=max(current.updated_at or current.created_at, now),
            ),
        )

    def get_chat(self, workspace_id: str, chat_id: str) -> ChatThread:
        chat = self._require(ChatThread, workspace_id, chat_id)
        if chat.state is ChatState.DELETED:
            raise RecordNotFound("conversation has been deleted")
        return chat

    def list_chats(self, workspace_id: str) -> tuple[ChatThread, ...]:
        return self._list(ChatThread, workspace_id)

    def admit_conversation_dispatch(self, workspace_id: str, chat_id: str) -> bool:
        """Claim already-in-flight work atomically against logical deletion."""
        assert_owned()
        with (
            self.mutation_guard(workspace_id, "intake-ready:" + chat_id),
            self._chat_record_guard(workspace_id, chat_id),
        ):
            try:
                self.get_chat(workspace_id, chat_id)
            except RecordNotFound:
                return False
            return True

    def archive_chat(self, workspace_id: str, chat_id: str) -> ChatThread:
        now = self._now()
        return self._update_chat(
            workspace_id,
            chat_id,
            lambda chat: (
                chat
                if chat.state is ChatState.ARCHIVED
                else replace_record(
                    chat,
                    state=ChatState.ARCHIVED,
                    updated_at=max(chat.updated_at or chat.created_at, now),
                )
            ),
        )

    def read_consumed_input(
        self, workspace_id: str, chat_id: str, anchor_id: str
    ) -> int:
        """Read the durable consumption fence; the root anchor is ``""``.

        An existing chat with no acknowledgement returns zero. Missing chats
        raise ``RecordNotFound``. The immutable root is read without a lease.
        """
        chat = self.get_chat(workspace_id, chat_id)
        return chat.input_consumed_positions.get(anchor_id, 0)

    def acknowledge_input(
        self, workspace_id: str, chat_id: str, anchor_id: str, position: int
    ) -> None:
        """Persist the maximum observed position without losing other anchors.

        The adapter retries a pure merge against the current root revision.
        No scheduling or tool calls occur here.
        """
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("consumed input position must be a non-negative integer")

        now = self._now()

        def acknowledge(chat: ChatThread) -> ChatThread:
            if position <= chat.input_consumed_positions.get(anchor_id, 0):
                return chat
            positions = dict(chat.input_consumed_positions)
            positions[anchor_id] = position
            return replace_record(
                chat,
                input_consumed_positions=positions,
                updated_at=max(chat.updated_at or chat.created_at, now),
            )

        self._update_chat(workspace_id, chat_id, acknowledge)

    def delete_chat(self, workspace_id: str, chat_id: str) -> None:
        """Durably hide/fence the entire root; physical cleanup is asynchronous."""
        self._persistence.mark_conversation_deleted(workspace_id, chat_id, self._now())
        self._enqueue_purge(workspace_id, chat_id)

    def resume_conversation_cleanup(self, workspace_id: str) -> None:
        for root in self._persistence.list_deleted_conversations(workspace_id):
            self._enqueue_purge(workspace_id, root.chat_id)

    def _enqueue_purge(self, workspace_id: str, chat_id: str) -> None:
        with self._cleanup_guard:
            key = (workspace_id, chat_id)
            if key in self._cleanup_pending:
                return
            self._cleanup_pending.add(key)
            self._cleanup_queue.put(key)
            if self._cleanup_worker is None:
                self._cleanup_worker = Thread(
                    target=self._purge_worker, name="conversation-cleanup", daemon=True
                )
                self._cleanup_worker.start()

    def _purge_worker(self) -> None:
        while True:
            workspace_id, chat_id = self._cleanup_queue.get()
            try:
                complete = self._persistence.purge_conversation(
                    workspace_id, chat_id, limit=50
                )
            except Exception as error:  # noqa: BLE001 - durable tombstone survives restart
                logging.getLogger(__name__).warning(
                    "conversation cleanup deferred",
                    extra={"error_type": type(error).__name__},
                )
                time.sleep(1)
                complete = False
            with self._cleanup_guard:
                if complete:
                    self._cleanup_pending.discard((workspace_id, chat_id))
                else:
                    self._cleanup_queue.put((workspace_id, chat_id))
            self._cleanup_queue.task_done()
            time.sleep(0.01)

    def _cancel_chat_sub_agents(
        self, workspace_id: str, chat_id: str
    ) -> tuple[str, ...]:
        """Cancel one chat's durable sub-agent forest, descendants included.

        Records are cancelled, never deleted: a background node turn reads the
        durable status as its cooperative cancellation fence, so removing the
        record would remove the fence while the turn is still in flight.
        Terminal records are left untouched.
        """
        records = self._list(SubAgentRecord, workspace_id)
        roots = [record.sub_agent_id for record in records if record.chat_id == chat_id]
        children: dict[str | None, list[str]] = {}
        for record in records:
            children.setdefault(record.parent_sub_agent_id, []).append(
                record.sub_agent_id
            )
        order: list[str] = []
        seen: set[str] = set()
        stack = list(reversed(roots))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            order.append(current)
            stack.extend(reversed(children.get(current, ())))
        by_id = {record.sub_agent_id: record for record in records}
        now = self._now()
        cancelled: list[str] = []
        for node_id in order:
            record = by_id[node_id]
            if record.status in _TERMINAL_SUB_AGENT_STATUSES:
                continue
            self._save(
                replace_record(
                    record,
                    status=SubAgentStatus.CANCELLED,
                    accepts_input=False,
                    updated_at=now,
                )
            )
            cancelled.append(node_id)
        return tuple(cancelled)

    def create_message(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        body: str,
        role: MessageRole = MessageRole.USER,
        reasoning: str | None = None,
        tool_result: Mapping[str, object] | None = None,
        attachment_ids: tuple[str, ...] = (),
        references: Mapping[str, tuple[str, ...]] | None = None,
        execution_id: str | None = None,
        intent_epoch: int = 0,
        kind: TranscriptItemKind = TranscriptItemKind.MESSAGE,
        data: Mapping[str, object] | None = None,
        message_id: str | None = None,
    ) -> ChatMessage:
        """Persist an accepted message and verify every workspace link.

        ``reasoning`` is an optional chain-of-thought side channel persisted
        alongside, but never inside, the answer ``body``.  ``tool_result`` is
        the structured harness-tool outcome carried by a ``tool``-role message
        so the conversation retains an ordered, renderable record of the call.
        ``kind``/``data`` let one conversation item discriminate between
        message, tool, sub-agent, and artifact payloads
        while every existing message keeps the ``message`` default.
        """
        if message_id is not None and (
            not isinstance(message_id, str) or not message_id.strip()
        ):
            raise ValueError("message_id must be non-empty text")
        anchor_id = (data or {}).get("subAgentId")
        if anchor_id is not None and (not isinstance(anchor_id, str) or not anchor_id):
            raise ValueError("subAgentId must be non-empty text or null")
        if role is MessageRole.ASSISTANT and not (data or {}).get("activityKind"):
            from thesis_runtime.contracts.activity import ModelOutput, activity_data

            data = {
                **dict(data or {}),
                **activity_data(ModelOutput(anchor_id)),
            }
        canonical = (data or {}).get("sessionMessage")
        if isinstance(canonical, Mapping):
            from thesis_runtime.contracts.messages import SessionMessage

            SessionMessage.from_dict(canonical)
            if message_id is None or canonical.get("message_id") != message_id:
                raise ValueError("canonical message identity must match message_id")
        execution = None
        if execution_id is not None:
            execution = self.get_execution(workspace_id, execution_id)
            if execution.chat_id != chat_id:
                raise CrossWorkspaceLinkError(
                    "execution does not belong to the selected chat"
                )
        attachments = self._existing_many(
            Attachment, workspace_id, tuple(attachment_ids)
        )
        for attachment in attachments:
            if attachment.chat_id != chat_id:
                raise CrossWorkspaceLinkError(
                    "attachment does not belong to the selected chat"
                )
        now = self._now()
        anchor = (data or {}).get("subAgentId")
        parent_conversation_id = None
        if isinstance(anchor, str) and anchor:
            node = self._persistence.get(SubAgentRecord, workspace_id, anchor)
            parent_conversation_id = node.parent_conversation_id if node else chat_id
        message = ChatMessage(
            workspace_id=workspace_id,
            created_at=now,
            created_by=actor_id,
            message_id=message_id
            if message_id is not None
            else self._id_factory("message"),
            chat_id=chat_id,
            conversation_id=anchor if isinstance(anchor, str) and anchor else chat_id,
            parent_conversation_id=parent_conversation_id,
            actor_id=actor_id,
            role=role,
            body=body,
            reasoning=reasoning,
            tool_result=tool_result,
            kind=kind,
            data=data,
            attachment_ids=tuple(
                attachment.attachment_id for attachment in attachments
            ),
            references=references or {},
            execution_id=None if execution is None else execution.execution_id,
            intent_epoch=intent_epoch,
            updated_at=now,
            root_conversation_id=chat_id,
        )
        if not isinstance(canonical, Mapping) and role is MessageRole.ASSISTANT:
            from thesis_runtime.contracts.messages import MessageContent, SessionMessage

            canonical = SessionMessage(
                message.message_id,
                "outgoing",
                MessageContent(
                    message.body,
                    "agent",
                    result={"reasoning": message.reasoning}
                    if message.reasoning
                    else None,
                ),
            ).as_dict()
            message = replace_record(
                message,
                data={**dict(message.data or {}), "sessionMessage": canonical},
            )

        # Admission validates lifecycle and commits against the root revision;
        # ACK and lifecycle updates use that same conditional storage boundary.
        if is_external_input(message):
            accepted_at = self._now()
            message = replace_record(
                message, created_at=accepted_at, updated_at=accepted_at
            )
        self._require_live_workspace(message.workspace_id)
        return self._persistence.admit_message(message)

    def send_message(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        body: str,
        role: MessageRole = MessageRole.USER,
        reasoning: str | None = None,
        tool_result: Mapping[str, object] | None = None,
        attachment_ids: tuple[str, ...] = (),
        references: Mapping[str, tuple[str, ...]] | None = None,
        execution_id: str | None = None,
        intent_epoch: int = 0,
        kind: TranscriptItemKind = TranscriptItemKind.MESSAGE,
        data: Mapping[str, object] | None = None,
    ) -> ChatMessage:
        """Store incoming messages canonically, or explicit assistant output."""
        if role is MessageRole.ASSISTANT and tool_result is None:
            from thesis_runtime.contracts.messages import SessionMessage

            if data is not None and "sessionMessage" in data:
                canonical = data["sessionMessage"]
                if not isinstance(canonical, Mapping):
                    raise ValueError("sessionMessage must be a mapping")
                message = SessionMessage.from_dict(canonical)
                if message.direction != "outgoing":
                    raise ValueError("assistant output requires an outgoing message")
                message_id = message.message_id
            else:
                message_id = None
            return self.create_message(
                workspace_id,
                chat_id,
                actor_id=actor_id,
                body=body,
                role=role,
                reasoning=reasoning,
                tool_result=tool_result,
                attachment_ids=attachment_ids,
                references=references,
                execution_id=execution_id,
                intent_epoch=intent_epoch,
                kind=kind,
                data=data,
                message_id=message_id,
            )
        if reasoning is not None:
            raise ValueError("incoming messages cannot contain reasoning")
        return self._accept_input(
            workspace_id,
            chat_id,
            actor_id=actor_id,
            body=body,
            role=role,
            tool_result=tool_result,
            attachment_ids=attachment_ids,
            references=references,
            execution_id=execution_id,
            intent_epoch=intent_epoch,
            kind=kind,
            data=data,
        )

    def _accept_input(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        body: str,
        role: MessageRole = MessageRole.USER,
        tool_result: Mapping[str, object] | None = None,
        attachment_ids: tuple[str, ...] = (),
        references: Mapping[str, tuple[str, ...]] | None = None,
        execution_id: str | None = None,
        intent_epoch: int = 0,
        kind: TranscriptItemKind = TranscriptItemKind.MESSAGE,
        data: Mapping[str, object] | None = None,
    ) -> ChatMessage:
        """Converge storage-only convenience intake on the incoming writer."""
        # Local imports avoid the runtime's dependency on this storage service.
        from thesis_runtime.contracts.messages import MessageContent, SessionMessage
        from thesis_runtime.conversations.delivery import Anchor
        from thesis_runtime.conversations.incoming import write_incoming

        if role not in (MessageRole.USER, MessageRole.TOOL, MessageRole.SYSTEM):
            raise ValueError("convenience intake requires an incoming role")
        supplied = data or {}
        canonical = supplied.get("sessionMessage")
        if "sessionMessage" in supplied:
            if not isinstance(canonical, Mapping):
                raise ValueError("sessionMessage must be a mapping")
            message = SessionMessage.from_dict(canonical)
            if message.direction != "incoming":
                raise ValueError("incoming intake requires an incoming message")
        else:
            if role is MessageRole.SYSTEM:
                source = "runtime"
            elif tool_result is not None:
                datum_type = tool_result.get("datumType")
                source = (
                    datum_type
                    if isinstance(datum_type, str) and datum_type
                    else "tool_result"
                )
            elif (
                supplied.get("toolActivity")
                or supplied.get("activityKind") == "runtime_tool"
            ):
                source = "runtime"
            elif role is MessageRole.TOOL or supplied.get("deliveryKey"):
                source = "delivery"
            else:
                source = "user"
            message = SessionMessage(
                self._id_factory("message"),
                "incoming",
                MessageContent(body, source, result=tool_result),
            )
        anchor_id = supplied.get("subAgentId")
        if anchor_id is not None and (not isinstance(anchor_id, str) or not anchor_id):
            raise ValueError("subAgentId must be non-empty text or null")
        return write_incoming(
            self,
            Anchor(workspace_id, chat_id, anchor_id),
            message,
            actor_id=actor_id,
            body=body,
            role=role,
            kind=kind,
            data=data,
            tool_result=tool_result,
            execution_id=execution_id,
            attachment_ids=attachment_ids,
            references=references,
            intent_epoch=intent_epoch,
        )

    def get_message(self, workspace_id: str, message_id: str) -> ChatMessage:
        return self._require(ChatMessage, workspace_id, message_id)

    def list_messages(self, workspace_id: str, chat_id: str) -> tuple[ChatMessage, ...]:
        self.get_chat(workspace_id, chat_id)
        messages = self._persistence.list_conversation(
            ChatMessage, workspace_id, chat_id
        )
        # Conversation order is chronological; a stable sort keeps insertion
        # order for records that share one clock tick.
        from thesis_runtime.conversations.admission import message_order_key

        return tuple(sorted(messages, key=message_order_key))

    def list_conversation_messages(
        self, workspace_id: str, chat_id: str, conversation_id: str
    ) -> tuple[ChatMessage, ...]:
        self.get_chat(workspace_id, chat_id)
        return self._persistence.list_conversation(
            ChatMessage, workspace_id, chat_id, conversation_id=conversation_id
        )

    def read_message_window(
        self,
        workspace_id: str,
        chat_id: str,
        conversation_id: str,
        *,
        after_position: int | None = None,
        consumed_position: int = 0,
        limit: int = 40,
    ) -> tuple[ChatMessage, ...]:
        """Load bounded history plus pending inputs, or inputs after a cursor."""
        self.get_chat(workspace_id, chat_id)
        return self._persistence.read_message_window(
            workspace_id,
            chat_id,
            conversation_id,
            after_position=after_position,
            consumed_position=consumed_position,
            limit=limit,
        )

    def list_attachments(
        self, workspace_id: str, chat_id: str
    ) -> tuple[Attachment, ...]:
        """Return upload metadata for one chat in deterministic order."""
        self.get_chat(workspace_id, chat_id)
        return self._persistence.list_conversation(Attachment, workspace_id, chat_id)

    def list_chat_executions(
        self, workspace_id: str, chat_id: str
    ) -> tuple[WorkflowExecution, ...]:
        self.get_chat(workspace_id, chat_id)
        return self._persistence.list_conversation(
            WorkflowExecution, workspace_id, chat_id, conversation_id=chat_id
        )

    def list_chat_sub_agents(
        self, workspace_id: str, chat_id: str
    ) -> tuple[SubAgentRecord, ...]:
        self.get_chat(workspace_id, chat_id)
        return self._persistence.list_conversation(
            SubAgentRecord, workspace_id, chat_id
        )

    def transition_message(
        self,
        workspace_id: str,
        message_id: str,
        state: MessageState,
    ) -> ChatMessage:
        message = self.get_message(workspace_id, message_id)
        if message.state is state:
            return message
        allowed = {
            MessageState.DRAFT: {MessageState.ACCEPTED, MessageState.DELETED},
            MessageState.ACCEPTED: {
                MessageState.STREAMING,
                MessageState.COMPLETED,
                MessageState.FAILED,
                MessageState.DELETED,
            },
            MessageState.STREAMING: {
                MessageState.COMPLETED,
                MessageState.FAILED,
                MessageState.DELETED,
            },
            MessageState.COMPLETED: {MessageState.DELETED},
            MessageState.FAILED: {MessageState.DELETED},
            MessageState.DELETED: set(),
        }
        if state not in allowed[message.state]:
            raise InvalidLifecycleTransition(
                f"message cannot move from {message.state.value} to {state.value}"
            )
        return self._save(replace_record(message, state=state, updated_at=self._now()))

    def accept_message(self, workspace_id: str, message_id: str) -> ChatMessage:
        return self.transition_message(workspace_id, message_id, MessageState.ACCEPTED)

    def start_message(self, workspace_id: str, message_id: str) -> ChatMessage:
        return self.transition_message(workspace_id, message_id, MessageState.STREAMING)

    def complete_message(self, workspace_id: str, message_id: str) -> ChatMessage:
        return self.transition_message(workspace_id, message_id, MessageState.COMPLETED)

    def fail_message(self, workspace_id: str, message_id: str) -> ChatMessage:
        return self.transition_message(workspace_id, message_id, MessageState.FAILED)

    def delete_message(self, workspace_id: str, message_id: str) -> ChatMessage:
        return self.transition_message(workspace_id, message_id, MessageState.DELETED)

    def create_attachment(
        self,
        workspace_id: str,
        chat_id: str,
        message_id: str,
        *,
        actor_id: str,
        filename: str,
        media_type: str,
        size_bytes: int,
    ) -> Attachment:
        """Create upload metadata and link it to its message."""
        self.get_chat(workspace_id, chat_id)
        message = self.get_message(workspace_id, message_id)
        if message.chat_id != chat_id:
            raise CrossWorkspaceLinkError(
                "message does not belong to the selected chat"
            )
        now = self._now()
        attachment = Attachment(
            workspace_id=workspace_id,
            created_at=now,
            created_by=actor_id,
            attachment_id=self._id_factory("attachment"),
            chat_id=chat_id,
            message_id=message_id,
            filename=filename,
            media_type=media_type,
            size_bytes=size_bytes,
            updated_at=now,
            conversation_id=chat_id,
            root_conversation_id=chat_id,
            parent_conversation_id=None,
        )
        self._save(attachment)
        self._save(
            replace_record(
                message,
                attachment_ids=(*message.attachment_ids, attachment.attachment_id),
                updated_at=now,
            )
        )
        return attachment

    def get_attachment(self, workspace_id: str, attachment_id: str) -> Attachment:
        return self._require(Attachment, workspace_id, attachment_id)

    def transition_attachment(
        self,
        workspace_id: str,
        attachment_id: str,
        state: AttachmentState,
        *,
        sha256: str | None = None,
        content_uri: str | None = None,
        error_message: str | None = None,
    ) -> Attachment:
        attachment = self.get_attachment(workspace_id, attachment_id)
        if attachment.state is state and all(
            value is None for value in (sha256, content_uri, error_message)
        ):
            return attachment
        allowed = {
            AttachmentState.PENDING: {
                AttachmentState.UPLOADING,
                AttachmentState.REJECTED,
                AttachmentState.DELETED,
            },
            AttachmentState.UPLOADING: {
                AttachmentState.UPLOADED,
                AttachmentState.REJECTED,
            },
            AttachmentState.UPLOADED: {
                AttachmentState.SCANNING,
                AttachmentState.REJECTED,
            },
            AttachmentState.SCANNING: {
                AttachmentState.AVAILABLE,
                AttachmentState.REJECTED,
            },
            AttachmentState.AVAILABLE: {AttachmentState.DELETED},
            AttachmentState.REJECTED: {AttachmentState.DELETED},
            AttachmentState.DELETED: set(),
        }
        if state is not attachment.state and state not in allowed[attachment.state]:
            raise InvalidLifecycleTransition(
                f"attachment cannot move from {attachment.state.value} to {state.value}"
            )
        return self._save(
            replace_record(
                attachment,
                state=state,
                sha256=attachment.sha256 if sha256 is None else sha256,
                content_uri=attachment.content_uri
                if content_uri is None
                else content_uri,
                error_message=(
                    attachment.error_message if error_message is None else error_message
                ),
                updated_at=self._now(),
            )
        )

    def upload_attachment(
        self,
        workspace_id: str,
        attachment_id: str,
        *,
        content_uri: str,
        sha256: str,
    ) -> Attachment:
        """Record a completed upload before content scanning begins."""
        attachment = self.transition_attachment(
            workspace_id,
            attachment_id,
            AttachmentState.UPLOADED,
            content_uri=content_uri,
            sha256=sha256,
        )
        return attachment

    def scan_attachment(
        self,
        workspace_id: str,
        attachment_id: str,
        *,
        available: bool,
        error_message: str | None = None,
    ) -> Attachment:
        """Move uploaded content through scanning to available or rejected."""
        attachment = self.get_attachment(workspace_id, attachment_id)
        if attachment.state is AttachmentState.UPLOADED:
            attachment = self.transition_attachment(
                workspace_id, attachment_id, AttachmentState.SCANNING
            )
        return self.transition_attachment(
            workspace_id,
            attachment_id,
            AttachmentState.AVAILABLE if available else AttachmentState.REJECTED,
            error_message=error_message,
        )

    def launch_workflow(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        workflow: WorkflowDefinition,
        actor_id: str,
        body: str | None = None,
        message_id: str | None = None,
        references: Mapping[str, tuple[str, ...]] | None = None,
    ) -> WorkflowExecution:
        """Persist the trigger and launch a durable execution from a chat."""
        self.get_chat(workspace_id, chat_id)
        if message_id is None:
            if body is None:
                raise ValueError("body or message_id is required to launch a workflow")
            trigger = self._accept_input(
                workspace_id,
                chat_id,
                actor_id=actor_id,
                body=body,
                references=references or {},
            )
        else:
            trigger = self.get_message(workspace_id, message_id)
            if trigger.chat_id != chat_id:
                raise CrossWorkspaceLinkError(
                    "trigger message does not belong to the selected chat"
                )
        now = self._now()
        execution = WorkflowExecution(
            workspace_id=workspace_id,
            created_at=now,
            created_by=actor_id,
            execution_id=self._id_factory("execution"),
            chat_id=chat_id,
            conversation_id=trigger.conversation_id,
            parent_conversation_id=trigger.parent_conversation_id,
            trigger_message_id=trigger.message_id,
            workflow_id=workflow.workflow_id,
            workflow_version=workflow.version,
            requested_by=actor_id,
            references=references or {},
            active_phases=workflow.phases[:1],
            # Chat launches have an immediately dedicated worker. There is no
            # shared workflow queue, so accepted executions are active rather
            # than advertising a false queued state while the task starts.
            state=WorkflowExecutionState.RUNNING,
            updated_at=now,
            root_conversation_id=chat_id,
        )
        with self.execution_guard(workspace_id, execution.execution_id):
            self._save(execution)
        self._save(replace_record(trigger, execution_id=execution.execution_id))
        self._update_chat(
            workspace_id,
            chat_id,
            lambda current: replace_record(
                current,
                links=current.links.add(
                    workflow_execution_ids=(execution.execution_id,),
                    references=references or {},
                ),
                updated_at=now,
            ),
        )
        self._append_event(
            execution,
            ExecutionEventType.MESSAGE_ACCEPTED,
            correlation_id=trigger.message_id,
            actor_id=trigger.actor_id,
            actor_scope=ActorScope.USER,
            payload={"messageId": trigger.message_id},
            intent_epoch=trigger.intent_epoch,
        )
        self._append_event(
            execution,
            ExecutionEventType.WORKFLOW_LAUNCHED,
            correlation_id=execution.execution_id,
            actor_id=actor_id,
            actor_scope=ActorScope.USER,
            payload={
                "chatId": chat_id,
                "triggerMessageId": trigger.message_id,
                "workflow": workflow.as_dict(),
            },
        )
        self._append_event(
            execution,
            ExecutionEventType.WORKFLOW_STARTED,
            correlation_id=execution.execution_id,
            actor_id=actor_id,
            actor_scope=ActorScope.WORKFLOW,
            payload={"state": execution.state.value},
        )
        return execution

    def get_execution(self, workspace_id: str, execution_id: str) -> WorkflowExecution:
        return self._require(WorkflowExecution, workspace_id, execution_id)

    def list_executions(self, workspace_id: str) -> tuple[WorkflowExecution, ...]:
        """Return the chat-level executions: only those launched by the chat.

        An execution launched by a sub-agent belongs to that node and is
        excluded here; use :meth:`list_node_executions` to find it under its
        anchor.
        """
        anchors = self._execution_anchors(workspace_id)
        active_roots = {root.chat_id for root in self.list_chats(workspace_id)}
        return tuple(
            execution
            for execution in self._list(WorkflowExecution, workspace_id)
            if execution.chat_id in active_roots
            and anchors.get(execution.execution_id) is None
        )

    def list_node_executions(
        self, workspace_id: str, sub_agent_id: str
    ) -> tuple[WorkflowExecution, ...]:
        """Return the executions owned by one sub-agent anchor."""
        anchors = self._execution_anchors(workspace_id)
        active_roots = {root.chat_id for root in self.list_chats(workspace_id)}
        return tuple(
            execution
            for execution in self._list(WorkflowExecution, workspace_id)
            if execution.chat_id in active_roots
            and anchors.get(execution.execution_id) == sub_agent_id
        )

    def execution_anchor(self, workspace_id: str, execution_id: str) -> str | None:
        """Return the anchor that launched an execution, or ``None`` for a chat."""
        self.get_execution(workspace_id, execution_id)
        return self._execution_anchors(workspace_id).get(execution_id)

    def _execution_anchors(self, workspace_id: str) -> dict[str, str | None]:
        anchors: dict[str, str | None] = {}
        for record in self._list(SubAgentRecord, workspace_id):
            if (
                record.role == WORKFLOW_NODE_ROLE
                and record.workflow_execution_id is not None
            ):
                anchors[record.workflow_execution_id] = record.parent_sub_agent_id
        return anchors

    def stop_active_executions(
        self, workspace_id: str, *, actor_id: str = "thesis-startup"
    ) -> tuple[WorkflowExecution, ...]:
        """Stop queued/running executions stranded by a server restart.

        Execution state is durable, but the worker that owned an in-process
        execution is not. Marking those records stopped at startup prevents the
        UI from waiting on work that no longer has a worker and prevents a
        restarted service from treating stale queued work as live.
        """
        active_roots = {root.chat_id for root in self.list_chats(workspace_id)}
        active = tuple(
            execution
            for execution in self._list(WorkflowExecution, workspace_id)
            if execution.chat_id in active_roots
            and execution.state
            in {WorkflowExecutionState.QUEUED, WorkflowExecutionState.RUNNING}
        )
        return tuple(
            self.stop_workflow(
                workspace_id,
                execution.execution_id,
                actor_id=actor_id,
            )
            for execution in active
        )

    def post_steering_message(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        body: str,
        role: MessageRole = MessageRole.USER,
        attachment_ids: tuple[str, ...] = (),
        references: Mapping[str, tuple[str, ...]] | None = None,
        targets_execution_id: str | None = None,
        steering_intent: str | None = None,
        message_factory: Callable[..., ChatMessage] | None = None,
        after_delivery: Callable[[ChatMessage], None] | None = None,
    ) -> SteeringPercolation:
        """Post a chat message and percolate its steering intent downward.

        The chat is the sole messaging surface: the message is persisted first
        as a first-class transcript entry and doubles as the durable steering
        acknowledgement.  When steering fields are present, every targeted
        (or, without an explicit target, every active) execution of the chat
        has its intent epoch bumped once and a ``workflow.steered`` event
        signals the orchestrator to replan at the next safe model or tool
        boundary.  Late tool results attach as evidence through
        :meth:`record_tool_result` without being discarded.
        """
        with self.mutation_guard(workspace_id, "intake-ready:" + chat_id):
            steering = targets_execution_id is not None or steering_intent is not None
            if steering and role is not MessageRole.USER:
                raise ValueError("only user messages can steer executions")
            targets: tuple[WorkflowExecution, ...] = ()
            if targets_execution_id is not None:
                target = self.get_execution(workspace_id, targets_execution_id)
                if target.chat_id != chat_id:
                    raise CrossWorkspaceLinkError(
                        "target execution does not belong to the selected chat"
                    )
                if target.state not in _STEERABLE_EXECUTION_STATES:
                    raise InvalidLifecycleTransition(
                        f"execution cannot be steered while {target.state.value}"
                    )
                targets = (target,)
            elif steering:
                targets = tuple(
                    execution
                    for execution in self._list(WorkflowExecution, workspace_id)
                    if execution.chat_id == chat_id
                    and execution.state in _STEERABLE_EXECUTION_STATES
                )
            next_epochs = [execution.intent_epoch + 1 for execution in targets]
            message_references = (
                references
                if references is not None
                else merge_references(*(execution.references for execution in targets))
            )

            def create(**fields: object) -> ChatMessage:
                return self._accept_input(
                    workspace_id,
                    chat_id,
                    actor_id=actor_id,
                    body=body,
                    role=role,
                    attachment_ids=attachment_ids,
                    references=message_references,
                    execution_id=targets_execution_id,
                    intent_epoch=max(next_epochs, default=0),
                )

            factory = message_factory or create
            message = factory(
                actor_id=actor_id,
                body=body,
                role=role,
                attachment_ids=attachment_ids,
                references=message_references,
                execution_id=targets_execution_id,
                intent_epoch=max(next_epochs, default=0),
            )
            percolated = tuple(
                self._apply_steering(
                    execution,
                    message,
                    intent_epoch=epoch,
                    steering_intent=steering_intent,
                )
                for execution, epoch in zip(targets, next_epochs, strict=True)
            )
            if after_delivery is not None:
                after_delivery(message)
            return SteeringPercolation(message=message, executions=percolated)

    def steer_workflow(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        actor_id: str,
        body: str,
        message_id: str | None = None,
        message_factory: Callable[..., ChatMessage] | None = None,
        after_delivery: Callable[[ChatMessage], None] | None = None,
    ) -> WorkflowExecution:
        """Steer one execution through its originating chat transcript.

        Internal primitive shared by the deprecated execution-scoped HTTP
        alias and workflow adapters.  New clients should post a steering chat
        message with ``targets_execution_id`` instead; this method still
        records every steering act as a chat message so transcripts hold all
        steering.
        """
        execution = self.get_execution(workspace_id, execution_id)
        if message_id is None:
            percolation = self.post_steering_message(
                workspace_id,
                execution.chat_id,
                actor_id=actor_id,
                body=body,
                targets_execution_id=execution_id,
                message_factory=message_factory,
                after_delivery=after_delivery,
            )
            return next(
                item
                for item in percolation.executions
                if item.execution_id == execution_id
            )
        message = self.get_message(workspace_id, message_id)
        if message.chat_id != execution.chat_id:
            raise CrossWorkspaceLinkError(
                "steering message does not belong to the execution chat"
            )
        if message.execution_id not in {None, execution_id}:
            raise CrossWorkspaceLinkError(
                "steering message belongs to another execution"
            )
        if message.execution_id is None:
            message = self._save(
                replace_record(
                    message, execution_id=execution_id, updated_at=self._now()
                )
            )
        updated = self._apply_steering(
            execution,
            message,
            intent_epoch=execution.intent_epoch + 1,
            steering_intent=None,
        )
        if after_delivery is not None:
            after_delivery(message)
        return updated

    def stop_workflow(
        self, workspace_id: str, execution_id: str, *, actor_id: str
    ) -> WorkflowExecution:
        return self._finish_workflow(
            workspace_id,
            execution_id,
            actor_id=actor_id,
            state=WorkflowExecutionState.STOPPED,
            event_type=ExecutionEventType.WORKFLOW_STOPPED,
        )

    def start_workflow(
        self, workspace_id: str, execution_id: str, *, actor_id: str
    ) -> WorkflowExecution:
        """Move a queued execution into its active running state."""
        with self.execution_guard(workspace_id, execution_id):
            execution = self.get_execution(workspace_id, execution_id)
            if execution.state is WorkflowExecutionState.RUNNING:
                return execution
            if execution.state is not WorkflowExecutionState.QUEUED:
                raise InvalidLifecycleTransition(
                    f"execution cannot start while {execution.state.value}"
                )
            updated = self._save(
                replace_record(
                    execution,
                    state=WorkflowExecutionState.RUNNING,
                    updated_at=self._now(),
                )
            )
        self._append_event(
            updated,
            ExecutionEventType.WORKFLOW_STARTED,
            correlation_id=updated.execution_id,
            actor_id=actor_id,
            actor_scope=ActorScope.WORKFLOW,
            payload={"state": updated.state.value},
        )
        return updated

    def set_execution_phases(
        self,
        workspace_id: str,
        execution_id: str,
        phases: tuple[str, ...],
        *,
        sequence: int | None = None,
    ) -> WorkflowExecution:
        """Project the accepted frontier without allowing older projections to win."""
        with self.execution_guard(workspace_id, execution_id):
            execution = self.get_execution(workspace_id, execution_id)
            if sequence is not None and sequence < execution.control_sequence:
                return execution
            if execution.active_phases == phases and sequence in (
                None,
                execution.control_sequence,
            ):
                return execution
            return self._save(
                replace_record(
                    execution,
                    active_phases=phases,
                    control_sequence=execution.control_sequence
                    if sequence is None
                    else sequence,
                    updated_at=self._now(),
                )
            )

    def accept_transition(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        expected_intent_epoch: int,
        expected_phase: str,
        expected_node_id: str | None,
        expected_attempt_id: str | None,
        commit: Callable[[], TransitionT],
        target_phases: tuple[str, ...] | None = None,
        target_sequence: int | None = None,
    ) -> TransitionT:
        """Validate live ownership, commit the store CAS, then project its frontier.

        Atomic against steering, termination and phase updates on this service
        owner only. ``commit`` must perform storage CAS only, never model/tool
        execution, conversation intake, or node-lock acquisition. Commit errors
        propagate without changing the frontier. Supply ``target_phases`` to fence
        duplicate acceptance before releasing the execution guard. Publication
        and downstream scheduling belong after this call, outside the guard.
        """
        with (
            self.execution_guard(workspace_id, execution_id),
            self._chat_record_guard(
                workspace_id, self.get_execution(workspace_id, execution_id).chat_id
            ),
        ):
            execution = self.get_execution(workspace_id, execution_id)
            if execution.state not in _STEERABLE_EXECUTION_STATES:
                raise InvalidLifecycleTransition(
                    "execution is no longer accepting completions"
                )
            if execution.intent_epoch != expected_intent_epoch:
                raise InvalidLifecycleTransition("completion intent epoch is stale")
            if expected_phase not in execution.active_phases:
                raise InvalidLifecycleTransition("completion phase is stale")
            if expected_node_id is not None:
                record = self._persistence.get(
                    SubAgentRecord, workspace_id, expected_node_id
                )
                owners = tuple(
                    node
                    for node in self._persistence.list_conversation(
                        SubAgentRecord, workspace_id, execution.chat_id
                    )
                    if node.role == WORKFLOW_NODE_ROLE
                    and node.workflow_execution_id == execution_id
                    and node.chat_id == execution.chat_id
                )
                if (
                    record is None
                    or not record.phase_executor
                    or record.workflow_execution_id != execution_id
                    or record.chat_id != execution.chat_id
                    or len(owners) != 1
                    or record.parent_sub_agent_id != owners[0].sub_agent_id
                    or record.phase != expected_phase
                    or record.phase_attempt_id != expected_attempt_id
                    or record.status in _TERMINAL_SUB_AGENT_STATUSES
                    or owners[0].status in _TERMINAL_SUB_AGENT_STATUSES
                ):
                    raise InvalidLifecycleTransition(
                        "completion does not belong to the current phase executor"
                    )
            elif expected_attempt_id is not None:
                raise InvalidLifecycleTransition("completion has no phase executor")
            result = commit()
            if target_phases is not None:
                self.set_execution_phases(
                    workspace_id, execution_id, target_phases, sequence=target_sequence
                )
            return result

    def cancel_workflow(
        self, workspace_id: str, execution_id: str, *, actor_id: str
    ) -> WorkflowExecution:
        return self._finish_workflow(
            workspace_id,
            execution_id,
            actor_id=actor_id,
            state=WorkflowExecutionState.CANCELLED,
            event_type=ExecutionEventType.WORKFLOW_CANCELLED,
        )

    def submit_tool_job(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        tool_name: str,
        actor_id: str,
        job_id: str | None = None,
        references: Mapping[str, tuple[str, ...]] | None = None,
        attribution: Mapping[str, object] | None = None,
    ) -> ToolJob:
        """Submit a tool job under one stable job identity.

        The record is Thesis's tracking attribution, submission certainty, and
        late-result observation boundary.  For a harness-backed operation the
        harness job record under the same ``job_id`` remains the execution-state
        and result authority.

        An explicit job id that is already registered joins the existing record
        only when its execution, operation, and attribution match; registration
        never resets timestamps, duplicates links, or overwrites a prior
        result.  Any mismatch fails as an explicit conflict.
        """
        now = self._now()
        submitted_job_id = self._id_factory("job") if job_id is None else job_id
        resolved_attribution: Mapping[str, object] = (
            {} if attribution is None else attribution
        )
        if job_id is not None:
            existing = self.find_tool_job(workspace_id, job_id)
            if existing is not None:
                return _join_tool_job(
                    existing,
                    execution_id=execution_id,
                    tool_name=tool_name,
                    attribution=resolved_attribution,
                    references=references,
                )
        with self.execution_guard(workspace_id, execution_id):
            execution = self.get_execution(workspace_id, execution_id)
            if execution.state not in _STEERABLE_EXECUTION_STATES:
                raise InvalidLifecycleTransition(
                    f"tool jobs cannot be submitted while {execution.state.value}"
                )
            job = ToolJob(
                workspace_id=workspace_id,
                created_at=now,
                created_by=actor_id,
                job_id=submitted_job_id,
                execution_id=execution_id,
                chat_id=execution.chat_id,
                conversation_id=execution.conversation_id,
                parent_conversation_id=execution.parent_conversation_id,
                tool_name=tool_name,
                submitted_by=actor_id,
                attribution=resolved_attribution,
                references=merge_references(execution.references, references or {}),
                intent_epoch=execution.intent_epoch,
                updated_at=now,
                root_conversation_id=execution.chat_id,
            )
            self._save(job)
            updated = self._save(
                replace_record(
                    execution,
                    references=merge_references(execution.references, references or {}),
                    job_ids=(*execution.job_ids, job.job_id),
                    updated_at=now,
                )
            )
        self._update_chat(
            workspace_id,
            execution.chat_id,
            lambda current: replace_record(
                current,
                links=current.links.add(
                    references=job.references,
                    job_ids=(job.job_id,),
                ),
                updated_at=now,
            ),
        )
        self._append_event(
            updated,
            ExecutionEventType.TOOL_SUBMITTED,
            correlation_id=job.job_id,
            actor_id=actor_id,
            actor_scope=ActorScope.WORKFLOW,
            payload={"jobId": job.job_id, "toolName": tool_name},
            intent_epoch=job.intent_epoch,
        )
        return job

    def get_tool_job(self, workspace_id: str, job_id: str) -> ToolJob:
        return self._require(ToolJob, workspace_id, job_id)

    def find_tool_job(self, workspace_id: str, job_id: str) -> ToolJob | None:
        """Return the tracking record for one job id, or ``None`` when untracked.

        Report and status readers use this boundary to distinguish a non-harness
        tool job, which must never be presented as a harness execution, from an
        untracked job the harness may still own.
        """
        if self._workspace_is_deleted(workspace_id):
            return None
        record = self._persistence.get(ToolJob, workspace_id, job_id)
        if record is None:
            return None
        if record.workspace_id != workspace_id:
            raise CrossWorkspaceLinkError("record belongs to another workspace")
        return record

    def find_execution_tool_job(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        tool_name: str,
        attribution: Mapping[str, object] | None = None,
    ) -> ToolJob | None:
        """Return the execution's tracked job matching operation and attribution.

        The execution's durable job links are the submission reference: resume
        and recovery rejoin the matching tracked job instead of allocating a
        replacement.  A stale job from another revision does not match and is
        left untouched.  More than one match is an ambiguous prior reference
        and fails closed rather than being guessed.
        """
        execution = self.get_execution(workspace_id, execution_id)
        expected = {} if attribution is None else dict(attribution)
        matches = [
            job
            for job in self._existing_many(ToolJob, workspace_id, execution.job_ids)
            if job.execution_id == execution_id
            and job.tool_name == tool_name
            and dict(job.attribution) == expected
        ]
        if len(matches) > 1:
            raise ToolJobConflictError(
                f"execution {execution_id!r} has {len(matches)} tracked "
                f"{tool_name!r} jobs with identical attribution"
            )
        return matches[0] if matches else None

    def start_tool_job(
        self, workspace_id: str, job_id: str, *, actor_id: str
    ) -> ToolJob:
        job = self.get_tool_job(workspace_id, job_id)
        with self.execution_guard(workspace_id, job.execution_id):
            job = self.get_tool_job(workspace_id, job_id)
            if job.state is ToolJobState.RUNNING:
                return job
            if job.state is not ToolJobState.SUBMITTED:
                raise InvalidLifecycleTransition(
                    f"tool job cannot start while {job.state.value}"
                )
            execution = self.get_execution(workspace_id, job.execution_id)
            updated = self._save(
                replace_record(job, state=ToolJobState.RUNNING, updated_at=self._now())
            )
        self._append_event(
            execution,
            ExecutionEventType.TOOL_STARTED,
            correlation_id=job.job_id,
            actor_id=actor_id,
            actor_scope=ActorScope.TOOL,
            payload={"jobId": job.job_id, "toolName": job.tool_name},
            intent_epoch=job.intent_epoch,
        )
        return updated

    def record_tool_result(
        self,
        workspace_id: str,
        job_id: str,
        *,
        result: Mapping[str, object] | None = None,
        actor_id: str | None = None,
        success: bool = True,
        error_message: str | None = None,
        intent_epoch: int | None = None,
    ) -> ToolJob:
        """Attach a result even when steering, stop, or cancel happened first."""
        job = self.get_tool_job(workspace_id, job_id)
        with self.execution_guard(workspace_id, job.execution_id):
            job = self.get_tool_job(workspace_id, job_id)
            execution = self.get_execution(workspace_id, job.execution_id)
            if job.state is ToolJobState.SUCCEEDED:
                return job
            observed_epoch = job.intent_epoch if intent_epoch is None else intent_epoch
            late = (
                job.late_result
                or execution.state
                in {
                    WorkflowExecutionState.STOPPED,
                    WorkflowExecutionState.CANCELLED,
                }
                or observed_epoch < execution.intent_epoch
            )
            updated = self._save(
                replace_record(
                    job,
                    state=ToolJobState.SUCCEEDED if success else ToolJobState.FAILED,
                    result=None if result is None else dict(result),
                    error_message=error_message,
                    late_result=late,
                    updated_at=self._now(),
                )
            )
        self._append_event(
            execution,
            ExecutionEventType.TOOL_RESULT
            if success
            else ExecutionEventType.TOOL_FAILED,
            correlation_id=job.job_id,
            actor_id=actor_id or job.tool_name,
            actor_scope=ActorScope.TOOL,
            payload={
                "jobId": job.job_id,
                "result": None if result is None else dict(result),
                "errorMessage": error_message,
            },
            intent_epoch=observed_epoch,
            late_result=late,
        )
        return updated

    def replay_events(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        after_cursor: int = 0,
        limit: int | None = None,
    ) -> EventReplay:
        """Replay events after an exclusive cursor for reconnecting clients."""
        self.get_execution(workspace_id, execution_id)
        replay = self._events.replay(
            execution_id, after_cursor=after_cursor, limit=limit
        )
        if any(event.workspace_id != workspace_id for event in replay.events):
            raise CrossWorkspaceLinkError("event stream crosses workspace boundaries")
        return replay

    def _apply_steering(
        self,
        execution: WorkflowExecution,
        message: ChatMessage,
        *,
        intent_epoch: int,
        steering_intent: str | None,
    ) -> WorkflowExecution:
        """Bump one execution's intent epoch for an acknowledged steering message."""
        with self.execution_guard(execution.workspace_id, execution.execution_id):
            execution = self.get_execution(
                execution.workspace_id, execution.execution_id
            )
            if execution.state not in _STEERABLE_EXECUTION_STATES:
                raise InvalidLifecycleTransition(
                    f"execution cannot be steered while {execution.state.value}"
                )
            intent_epoch = execution.intent_epoch + 1
            updated = self._save(
                replace_record(
                    execution,
                    intent_epoch=intent_epoch,
                    steering_message_ids=(
                        *execution.steering_message_ids,
                        message.message_id,
                    ),
                    updated_at=self._now(),
                )
            )
        self._append_event(
            updated,
            ExecutionEventType.WORKFLOW_STEERED,
            correlation_id=message.message_id,
            actor_id=message.actor_id,
            actor_scope=ActorScope.USER,
            payload={
                "chatId": message.chat_id,
                "messageId": message.message_id,
                "intentEpoch": intent_epoch,
                "steeringIntent": steering_intent,
            },
            intent_epoch=intent_epoch,
        )
        return updated

    def finish_workflow(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        actor_id: str,
        state: WorkflowExecutionState,
        event_type: ExecutionEventType,
        payload: Mapping[str, object] | None = None,
    ) -> bool:
        """Apply one terminal result under the execution's durable ownership guard.

        Completion, failure, stop and cancellation share this authority. A late
        outcome cannot overwrite an accepted terminal state.
        """
        if state not in {
            WorkflowExecutionState.COMPLETED,
            WorkflowExecutionState.FAILED,
            WorkflowExecutionState.STOPPED,
            WorkflowExecutionState.CANCELLED,
        }:
            raise InvalidLifecycleTransition(
                "finish requires a terminal execution state"
            )
        with self.execution_guard(workspace_id, execution_id):
            execution = self.get_execution(workspace_id, execution_id)
            if execution.state not in _STEERABLE_EXECUTION_STATES:
                return False
            now = self._now()
            updated = self._save(
                replace_record(execution, state=state, finished_at=now, updated_at=now)
            )
            self._append_event(
                updated,
                event_type,
                correlation_id=execution_id,
                actor_id=actor_id,
                actor_scope=ActorScope.WORKFLOW,
                payload={
                    **dict(payload or {"state": state.value}),
                    "activePhases": list(updated.active_phases),
                    "controlSequence": updated.control_sequence,
                },
                intent_epoch=updated.intent_epoch,
            )
        return True

    def _finish_workflow(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        actor_id: str,
        state: WorkflowExecutionState,
        event_type: ExecutionEventType,
    ) -> WorkflowExecution:
        with self.execution_guard(workspace_id, execution_id):
            current = self.get_execution(workspace_id, execution_id)
            if (
                current.state not in _STEERABLE_EXECUTION_STATES
                and current.state is not state
            ):
                raise InvalidLifecycleTransition(
                    f"execution cannot move from {current.state.value} to {state.value}"
                )
            self.finish_workflow(
                workspace_id,
                execution_id,
                actor_id=actor_id,
                state=state,
                event_type=event_type,
            )
            return self.get_execution(workspace_id, execution_id)

    def _append_event(
        self,
        execution: WorkflowExecution,
        event_type: ExecutionEventType,
        *,
        correlation_id: str,
        actor_id: str,
        actor_scope: ActorScope,
        payload: Mapping[str, object],
        intent_epoch: int | None = None,
        late_result: bool = False,
    ) -> ExecutionEvent:
        return self._events.append(
            ExecutionEvent(
                execution_id=execution.execution_id,
                workspace_id=execution.workspace_id,
                event_type=event_type,
                correlation_id=correlation_id,
                actor_id=actor_id,
                actor_scope=actor_scope,
                occurred_at=self._now(),
                payload=payload,
                intent_epoch=execution.intent_epoch
                if intent_epoch is None
                else intent_epoch,
                late_result=late_result,
            )
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("chat clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    def _save(self, record: RecordT) -> RecordT:
        self._require_live_workspace(record.workspace_id)
        return self._persistence.save(record)

    def _require(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> RecordT:
        self._require_live_workspace(workspace_id)
        record = self._persistence.get(record_type, workspace_id, record_id)
        if record is None:
            raise RecordNotFound(
                f"{record_type.__name__} {record_id!r} was not found in workspace "
                f"{workspace_id!r}"
            )
        if record.workspace_id != workspace_id:
            raise CrossWorkspaceLinkError("record belongs to another workspace")
        if isinstance(record, ConversationRecord) and not isinstance(
            record, ChatThread
        ):
            self.get_chat(workspace_id, record.chat_id)
        return record

    def _list(
        self, record_type: type[RecordT], workspace_id: str
    ) -> tuple[RecordT, ...]:
        if self._workspace_is_deleted(workspace_id):
            return ()
        return self._persistence.list(record_type, workspace_id)

    def _delete(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> None:
        self._persistence.delete(record_type, workspace_id, record_id)

    def _existing_many(
        self,
        record_type: type[RecordT],
        workspace_id: str,
        record_ids: tuple[str, ...],
    ) -> tuple[RecordT, ...]:
        return tuple(
            self._require(record_type, workspace_id, record_id)
            for record_id in record_ids
        )


def _join_tool_job(
    existing: ToolJob,
    *,
    execution_id: str,
    tool_name: str,
    attribution: Mapping[str, object],
    references: Mapping[str, tuple[str, ...]] | None,
) -> ToolJob:
    """Return an already-registered job, or fail closed on any mismatch."""
    if (
        existing.execution_id != execution_id
        or existing.tool_name != tool_name
        or dict(existing.attribution) != dict(attribution)
        or (
            references is not None
            and any(
                existing.references.get(key) != ids for key, ids in references.items()
            )
        )
    ):
        raise ToolJobConflictError(
            f"tool job {existing.job_id!r} is already registered with a different "
            "execution, operation, attribution, or resource references"
        )
    return existing


__all__ = [
    "ChatPersistence",
    "ChatService",
    "InMemoryChatPersistence",
    "SteeringPercolation",
]
