"""Application boundary for workspace chat and workflow lifecycle commands."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeVar, cast

from thesis.chat.contracts import (
    Attachment,
    AttachmentState,
    ChatMessage,
    ChatState,
    ChatThread,
    CrossWorkspaceLinkError,
    InvalidLifecycleTransition,
    Investigation,
    InvestigationState,
    MessageRole,
    MessageState,
    RecordLinks,
    RecordNotFound,
    ToolJob,
    ToolJobState,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowExecutionState,
    replace_record,
)
from thesis.execution.events import (
    ActorScope,
    EventReplay,
    ExecutionEvent,
    ExecutionEventStore,
    ExecutionEventType,
    InMemoryExecutionEventStore,
)

Record = (
    ChatThread | ChatMessage | Attachment | Investigation | WorkflowExecution | ToolJob
)
RecordType = type[Record]
RecordT = TypeVar("RecordT", bound=Record)

_STEERABLE_EXECUTION_STATES = frozenset(
    {
        WorkflowExecutionState.QUEUED,
        WorkflowExecutionState.RUNNING,
        WorkflowExecutionState.PAUSED,
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

    def get(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> RecordT | None: ...

    def list(
        self, record_type: type[RecordT], workspace_id: str
    ) -> tuple[RecordT, ...]: ...

    def delete(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> None: ...


class InMemoryChatPersistence:
    """Small deterministic persistence adapter suitable for local runtime tests."""

    _record_types: tuple[RecordType, ...] = (
        ChatThread,
        ChatMessage,
        Attachment,
        Investigation,
        WorkflowExecution,
        ToolJob,
    )

    def __init__(self, records: Iterable[Record] = ()) -> None:
        self._records: dict[type[Record], dict[tuple[str, str], Record]] = {
            record_type: {} for record_type in self._record_types
        }
        for record in records:
            self.save(record)

    def save(self, record: RecordT) -> RecordT:
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
        )
        return cast(tuple[RecordT, ...], values)

    def delete(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> None:
        self._records[record_type].pop((workspace_id, record_id), None)


def _record_id(record: Record) -> str:
    id_name = {
        ChatThread: "chat_id",
        ChatMessage: "message_id",
        Attachment: "attachment_id",
        Investigation: "investigation_id",
        WorkflowExecution: "execution_id",
        ToolJob: "tool_job_id",
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
    """Workspace-safe chat and workflow service with injected durable ports."""

    def __init__(
        self,
        persistence: ChatPersistence | None = None,
        *,
        events: ExecutionEventStore | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
    ) -> None:
        self._persistence = (
            InMemoryChatPersistence() if persistence is None else persistence
        )
        self._events = InMemoryExecutionEventStore() if events is None else events
        self._clock = clock
        self._id_factory = id_factory

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
        investigation_ids: tuple[str, ...] = (),
        strategy_ids: tuple[str, ...] = (),
        experiment_ids: tuple[str, ...] = (),
    ) -> ChatThread:
        """Create one chat and establish links to existing investigations."""
        links = RecordLinks(
            investigation_ids=investigation_ids,
            strategy_ids=strategy_ids,
            experiment_ids=experiment_ids,
        )
        investigations = self._existing_many(
            Investigation, workspace_id, links.investigation_ids
        )
        now = self._now()
        chat = ChatThread(
            workspace_id=workspace_id,
            created_at=now,
            created_by=owner_actor_id,
            chat_id=self._id_factory("chat"),
            title=title,
            owner_actor_id=owner_actor_id,
            links=links,
            updated_at=now,
        )
        self._save(chat)
        for investigation in investigations:
            self._save(
                replace_record(
                    investigation,
                    links=investigation.links.add(chat_ids=(chat.chat_id,)),
                    updated_at=now,
                )
            )
        return chat

    def get_chat(self, workspace_id: str, chat_id: str) -> ChatThread:
        return self._require(ChatThread, workspace_id, chat_id)

    def list_chats(self, workspace_id: str) -> tuple[ChatThread, ...]:
        return self._list(ChatThread, workspace_id)

    def archive_chat(self, workspace_id: str, chat_id: str) -> ChatThread:
        chat = self.get_chat(workspace_id, chat_id)
        if chat.state is ChatState.ARCHIVED:
            return chat
        return self._save(
            replace_record(chat, state=ChatState.ARCHIVED, updated_at=self._now())
        )

    def delete_chat(self, workspace_id: str, chat_id: str) -> None:
        """Remove a chat and the records it owns.

        Deletion cascades to the chat's messages and attachments and to any
        workflow executions and tool jobs launched from it, so the workspace
        retains no orphaned references to the conversation.
        """
        self.get_chat(workspace_id, chat_id)
        for message in self._list(ChatMessage, workspace_id):
            if message.chat_id == chat_id:
                self._delete(ChatMessage, workspace_id, message.message_id)
        for attachment in self._list(Attachment, workspace_id):
            if attachment.chat_id == chat_id:
                self._delete(Attachment, workspace_id, attachment.attachment_id)
        executions = [
            execution
            for execution in self._list(WorkflowExecution, workspace_id)
            if execution.chat_id == chat_id
        ]
        execution_ids = {execution.execution_id for execution in executions}
        for job in self._list(ToolJob, workspace_id):
            if job.execution_id in execution_ids:
                self._delete(ToolJob, workspace_id, job.tool_job_id)
        for execution in executions:
            self._delete(WorkflowExecution, workspace_id, execution.execution_id)
        self._delete(ChatThread, workspace_id, chat_id)

    def create_investigation(
        self,
        workspace_id: str,
        *,
        title: str,
        objective: str,
        owner_actor_id: str,
        chat_ids: tuple[str, ...] = (),
        strategy_ids: tuple[str, ...] = (),
        experiment_ids: tuple[str, ...] = (),
    ) -> Investigation:
        """Create an investigation and establish links to existing chats."""
        links = RecordLinks(
            chat_ids=chat_ids,
            strategy_ids=strategy_ids,
            experiment_ids=experiment_ids,
        )
        chats = self._existing_many(ChatThread, workspace_id, links.chat_ids)
        now = self._now()
        investigation = Investigation(
            workspace_id=workspace_id,
            created_at=now,
            created_by=owner_actor_id,
            investigation_id=self._id_factory("investigation"),
            title=title,
            objective=objective,
            owner_actor_id=owner_actor_id,
            links=links,
            updated_at=now,
        )
        self._save(investigation)
        for chat in chats:
            self._save(
                replace_record(
                    chat,
                    links=chat.links.add(
                        investigation_ids=(investigation.investigation_id,)
                    ),
                    updated_at=now,
                )
            )
        return investigation

    def get_investigation(
        self, workspace_id: str, investigation_id: str
    ) -> Investigation:
        return self._require(Investigation, workspace_id, investigation_id)

    def list_investigations(self, workspace_id: str) -> tuple[Investigation, ...]:
        return self._list(Investigation, workspace_id)

    def set_investigation_state(
        self,
        workspace_id: str,
        investigation_id: str,
        state: InvestigationState,
    ) -> Investigation:
        investigation = self.get_investigation(workspace_id, investigation_id)
        if investigation.state is state:
            return investigation
        allowed = {
            InvestigationState.OPEN: {
                InvestigationState.PAUSED,
                InvestigationState.COMPLETED,
                InvestigationState.ARCHIVED,
            },
            InvestigationState.PAUSED: {
                InvestigationState.OPEN,
                InvestigationState.ARCHIVED,
            },
            InvestigationState.COMPLETED: {InvestigationState.ARCHIVED},
            InvestigationState.ARCHIVED: set(),
        }
        if state not in allowed[investigation.state]:
            raise InvalidLifecycleTransition(
                f"investigation cannot move from {investigation.state.value} "
                f"to {state.value}"
            )
        return self._save(
            replace_record(investigation, state=state, updated_at=self._now())
        )

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
        investigation_ids: tuple[str, ...] = (),
        execution_id: str | None = None,
        intent_epoch: int = 0,
    ) -> ChatMessage:
        """Persist an accepted message and verify every workspace link.

        ``reasoning`` is an optional chain-of-thought side channel persisted
        alongside, but never inside, the answer ``body``.  ``tool_result`` is
        the structured harness-tool outcome carried by a ``tool``-role message
        so the conversation retains an ordered, renderable record of the call.
        """
        chat = self.get_chat(workspace_id, chat_id)
        if chat.state is ChatState.ARCHIVED:
            raise InvalidLifecycleTransition(
                "messages cannot be added to an archived chat"
            )
        investigations = self._existing_many(
            Investigation, workspace_id, tuple(investigation_ids)
        )
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
        message = ChatMessage(
            workspace_id=workspace_id,
            created_at=now,
            created_by=actor_id,
            message_id=self._id_factory("message"),
            chat_id=chat_id,
            actor_id=actor_id,
            role=role,
            body=body,
            reasoning=reasoning,
            tool_result=tool_result,
            attachment_ids=tuple(
                attachment.attachment_id for attachment in attachments
            ),
            investigation_ids=tuple(
                investigation.investigation_id for investigation in investigations
            ),
            execution_id=None if execution is None else execution.execution_id,
            intent_epoch=intent_epoch,
            updated_at=now,
        )
        self._save(message)
        self._save(
            replace_record(
                chat,
                links=chat.links.add(
                    investigation_ids=message.investigation_ids,
                    workflow_execution_ids=()
                    if execution is None
                    else (execution.execution_id,),
                ),
                updated_at=now,
            )
        )
        for investigation in investigations:
            self._save(
                replace_record(
                    investigation,
                    links=investigation.links.add(chat_ids=(chat_id,)),
                    updated_at=now,
                )
            )
        return message

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
        investigation_ids: tuple[str, ...] = (),
        execution_id: str | None = None,
        intent_epoch: int = 0,
    ) -> ChatMessage:
        """UI-friendly alias for :meth:`create_message`."""
        return self.create_message(
            workspace_id,
            chat_id,
            actor_id=actor_id,
            body=body,
            role=role,
            reasoning=reasoning,
            tool_result=tool_result,
            attachment_ids=attachment_ids,
            investigation_ids=investigation_ids,
            execution_id=execution_id,
            intent_epoch=intent_epoch,
        )

    def get_message(self, workspace_id: str, message_id: str) -> ChatMessage:
        return self._require(ChatMessage, workspace_id, message_id)

    def list_messages(self, workspace_id: str, chat_id: str) -> tuple[ChatMessage, ...]:
        self.get_chat(workspace_id, chat_id)
        messages = [
            message
            for message in self._list(ChatMessage, workspace_id)
            if message.chat_id == chat_id
        ]
        # Conversation order is chronological; a stable sort keeps insertion
        # order for records that share one clock tick.
        return tuple(sorted(messages, key=lambda message: message.created_at))

    def list_attachments(
        self, workspace_id: str, chat_id: str
    ) -> tuple[Attachment, ...]:
        """Return upload metadata for one chat in deterministic order."""
        self.get_chat(workspace_id, chat_id)
        return tuple(
            attachment
            for attachment in self._list(Attachment, workspace_id)
            if attachment.chat_id == chat_id
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
        investigation_id: str | None = None,
        strategy_ids: tuple[str, ...] = (),
        experiment_ids: tuple[str, ...] = (),
    ) -> WorkflowExecution:
        """Persist the trigger and launch a durable execution from a chat."""
        self.get_chat(workspace_id, chat_id)
        investigation = (
            None
            if investigation_id is None
            else self.get_investigation(workspace_id, investigation_id)
        )
        if message_id is None:
            if body is None:
                raise ValueError("body or message_id is required to launch a workflow")
            trigger = self.create_message(
                workspace_id,
                chat_id,
                actor_id=actor_id,
                body=body,
                investigation_ids=()
                if investigation is None
                else (investigation.investigation_id,),
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
            trigger_message_id=trigger.message_id,
            workflow_id=workflow.workflow_id,
            workflow_version=workflow.version,
            requested_by=actor_id,
            investigation_id=None
            if investigation is None
            else investigation.investigation_id,
            strategy_ids=strategy_ids,
            experiment_ids=experiment_ids,
            phase=workflow.phases[0] if workflow.phases else None,
            updated_at=now,
        )
        self._save(execution)
        self._save(replace_record(trigger, execution_id=execution.execution_id))
        chat = self.get_chat(workspace_id, chat_id)
        chat_links = chat.links.add(workflow_execution_ids=(execution.execution_id,))
        chat_links = chat_links.add(
            strategy_ids=strategy_ids,
            experiment_ids=experiment_ids,
        )
        if investigation is not None:
            chat_links = chat_links.add(
                investigation_ids=(investigation.investigation_id,)
            )
        self._save(replace_record(chat, links=chat_links, updated_at=now))
        if investigation is not None:
            self._save(
                replace_record(
                    investigation,
                    links=investigation.links.add(
                        chat_ids=(chat_id,),
                        workflow_execution_ids=(execution.execution_id,),
                        strategy_ids=strategy_ids,
                        experiment_ids=experiment_ids,
                    ),
                    updated_at=now,
                )
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
        return execution

    def get_execution(self, workspace_id: str, execution_id: str) -> WorkflowExecution:
        return self._require(WorkflowExecution, workspace_id, execution_id)

    def list_executions(self, workspace_id: str) -> tuple[WorkflowExecution, ...]:
        return self._list(WorkflowExecution, workspace_id)

    def post_steering_message(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        body: str,
        role: MessageRole = MessageRole.USER,
        attachment_ids: tuple[str, ...] = (),
        investigation_ids: tuple[str, ...] | None = None,
        targets_execution_id: str | None = None,
        steering_intent: str | None = None,
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
        if investigation_ids is None:
            derived: list[str] = [
                execution.investigation_id
                for execution in targets
                if execution.investigation_id is not None
            ]
            message_investigation_ids = tuple(dict.fromkeys(derived))
        else:
            message_investigation_ids = tuple(investigation_ids)
        message = self.create_message(
            workspace_id,
            chat_id,
            actor_id=actor_id,
            body=body,
            role=role,
            attachment_ids=attachment_ids,
            investigation_ids=message_investigation_ids,
            execution_id=None if targets_execution_id is None else targets_execution_id,
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
        return SteeringPercolation(message=message, executions=percolated)

    def steer_workflow(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        actor_id: str,
        body: str,
        message_id: str | None = None,
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
        return self._apply_steering(
            execution,
            message,
            intent_epoch=execution.intent_epoch + 1,
            steering_intent=None,
        )

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
        execution = self.get_execution(workspace_id, execution_id)
        if execution.state is WorkflowExecutionState.RUNNING:
            return execution
        if execution.state not in {
            WorkflowExecutionState.QUEUED,
            WorkflowExecutionState.PAUSED,
        }:
            raise InvalidLifecycleTransition(
                f"execution cannot start while {execution.state.value}"
            )
        updated = self._save(
            replace_record(
                execution, state=WorkflowExecutionState.RUNNING, updated_at=self._now()
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

    def pause_workflow(
        self, workspace_id: str, execution_id: str, *, actor_id: str
    ) -> WorkflowExecution:
        execution = self.get_execution(workspace_id, execution_id)
        if execution.state is WorkflowExecutionState.PAUSED:
            return execution
        if execution.state is not WorkflowExecutionState.RUNNING:
            raise InvalidLifecycleTransition(
                f"execution cannot pause while {execution.state.value}"
            )
        updated = self._save(
            replace_record(
                execution, state=WorkflowExecutionState.PAUSED, updated_at=self._now()
            )
        )
        self._append_event(
            updated,
            ExecutionEventType.WORKFLOW_PAUSED,
            correlation_id=updated.execution_id,
            actor_id=actor_id,
            actor_scope=ActorScope.USER,
            payload={"state": updated.state.value},
        )
        return updated

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
        correlation_id: str | None = None,
        investigation_id: str | None = None,
        strategy_ids: tuple[str, ...] = (),
        experiment_ids: tuple[str, ...] = (),
    ) -> ToolJob:
        """Submit a tool job while preserving its execution correlation."""
        execution = self.get_execution(workspace_id, execution_id)
        if execution.state in {
            WorkflowExecutionState.CANCELLED,
            WorkflowExecutionState.COMPLETED,
            WorkflowExecutionState.FAILED,
        }:
            raise InvalidLifecycleTransition(
                f"tool jobs cannot be submitted while {execution.state.value}"
            )
        if investigation_id is not None:
            if execution.investigation_id != investigation_id:
                raise CrossWorkspaceLinkError(
                    "tool job investigation does not match the execution"
                )
            self.get_investigation(workspace_id, investigation_id)
        now = self._now()
        job = ToolJob(
            workspace_id=workspace_id,
            created_at=now,
            created_by=actor_id,
            tool_job_id=self._id_factory("tool-job"),
            execution_id=execution_id,
            chat_id=execution.chat_id,
            correlation_id=(
                self._id_factory("correlation")
                if correlation_id is None
                else correlation_id
            ),
            tool_name=tool_name,
            submitted_by=actor_id,
            investigation_id=investigation_id or execution.investigation_id,
            strategy_ids=strategy_ids,
            experiment_ids=experiment_ids,
            intent_epoch=execution.intent_epoch,
            updated_at=now,
        )
        self._save(job)
        updated = replace_record(
            execution,
            strategy_ids=(*execution.strategy_ids, *strategy_ids),
            experiment_ids=(*execution.experiment_ids, *experiment_ids),
            tool_job_ids=(*execution.tool_job_ids, job.tool_job_id),
            updated_at=now,
        )
        self._save(updated)
        if updated.investigation_id is not None:
            investigation = self.get_investigation(
                workspace_id, updated.investigation_id
            )
            self._save(
                replace_record(
                    investigation,
                    links=investigation.links.add(
                        tool_job_ids=(job.tool_job_id,),
                        strategy_ids=strategy_ids,
                        experiment_ids=experiment_ids,
                    ),
                    updated_at=now,
                )
            )
        chat = self.get_chat(workspace_id, execution.chat_id)
        self._save(
            replace_record(
                chat,
                links=chat.links.add(
                    strategy_ids=strategy_ids,
                    experiment_ids=experiment_ids,
                    tool_job_ids=(job.tool_job_id,),
                ),
                updated_at=now,
            )
        )
        self._append_event(
            updated,
            ExecutionEventType.TOOL_SUBMITTED,
            correlation_id=job.correlation_id,
            actor_id=actor_id,
            actor_scope=ActorScope.WORKFLOW,
            payload={"toolJobId": job.tool_job_id, "toolName": tool_name},
            intent_epoch=job.intent_epoch,
        )
        return job

    def get_tool_job(self, workspace_id: str, tool_job_id: str) -> ToolJob:
        return self._require(ToolJob, workspace_id, tool_job_id)

    def start_tool_job(
        self, workspace_id: str, tool_job_id: str, *, actor_id: str
    ) -> ToolJob:
        job = self.get_tool_job(workspace_id, tool_job_id)
        if job.state is ToolJobState.RUNNING:
            return job
        if job.state is not ToolJobState.SUBMITTED:
            raise InvalidLifecycleTransition(
                f"tool job cannot start while {job.state.value}"
            )
        updated = self._save(
            replace_record(job, state=ToolJobState.RUNNING, updated_at=self._now())
        )
        execution = self.get_execution(workspace_id, job.execution_id)
        self._append_event(
            execution,
            ExecutionEventType.TOOL_STARTED,
            correlation_id=job.correlation_id,
            actor_id=actor_id,
            actor_scope=ActorScope.TOOL,
            payload={"toolJobId": job.tool_job_id, "toolName": job.tool_name},
            intent_epoch=job.intent_epoch,
        )
        return updated

    def record_tool_result(
        self,
        workspace_id: str,
        tool_job_id: str,
        *,
        result: Mapping[str, object] | None = None,
        actor_id: str | None = None,
        success: bool = True,
        error_message: str | None = None,
        intent_epoch: int | None = None,
    ) -> ToolJob:
        """Attach a result even when steering, stop, or cancel happened first."""
        job = self.get_tool_job(workspace_id, tool_job_id)
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
            correlation_id=job.correlation_id,
            actor_id=actor_id or job.tool_name,
            actor_scope=ActorScope.TOOL,
            payload={
                "toolJobId": job.tool_job_id,
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
        if execution.state not in _STEERABLE_EXECUTION_STATES:
            raise InvalidLifecycleTransition(
                f"execution cannot be steered while {execution.state.value}"
            )
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

    def _finish_workflow(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        actor_id: str,
        state: WorkflowExecutionState,
        event_type: ExecutionEventType,
    ) -> WorkflowExecution:
        execution = self.get_execution(workspace_id, execution_id)
        if execution.state is state:
            return execution
        if execution.state in {
            WorkflowExecutionState.COMPLETED,
            WorkflowExecutionState.FAILED,
            WorkflowExecutionState.STOPPED,
            WorkflowExecutionState.CANCELLED,
        }:
            raise InvalidLifecycleTransition(
                f"execution cannot move from {execution.state.value} to {state.value}"
            )
        updated = self._save(
            replace_record(
                execution, state=state, finished_at=self._now(), updated_at=self._now()
            )
        )
        self._append_event(
            updated,
            event_type,
            correlation_id=updated.execution_id,
            actor_id=actor_id,
            actor_scope=ActorScope.USER,
            payload={"state": state.value},
            intent_epoch=updated.intent_epoch,
        )
        return updated

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
        return self._persistence.save(record)

    def _require(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> RecordT:
        record = self._persistence.get(record_type, workspace_id, record_id)
        if record is None:
            raise RecordNotFound(
                f"{record_type.__name__} {record_id!r} was not found in workspace "
                f"{workspace_id!r}"
            )
        if record.workspace_id != workspace_id:
            raise CrossWorkspaceLinkError("record belongs to another workspace")
        return record

    def _list(
        self, record_type: type[RecordT], workspace_id: str
    ) -> tuple[RecordT, ...]:
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


__all__ = [
    "ChatPersistence",
    "ChatService",
    "InMemoryChatPersistence",
    "SteeringPercolation",
]
