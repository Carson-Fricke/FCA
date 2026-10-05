"""Filesystem-backed durable persistence for workspace chat records.

The chat service persists immutable record snapshots through the
:class:`~thesis.chat.service.ChatPersistence` port.  The in-memory adapter is
suitable for tests but loses every conversation when the process restarts; this
adapter writes each snapshot as one atomically-replaced JSON document under a
workspace-scoped root so the local stack survives restarts.

Layout::

    <root>/<RecordType>/<sha256(workspace_id NUL record_id)>.json

The filename is content-independent (a stable hash of the record identity), so
records can be overwritten in place and never leak record ids that contain path
separators.  ``list`` filters every document in a record-type directory by its
stored ``workspace_id``; the local dataset is small enough that a directory scan
is the simplest correct index.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any, TypeVar, cast

from thesis.chat.contracts import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
    Attachment,
    AttachmentState,
    ChatContractError,
    ChatMessage,
    ChatState,
    ChatThread,
    CompletionMode,
    ConversationRecord,
    Investigation,
    InvestigationState,
    MessageRole,
    MessageState,
    RecordLinks,
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
    ToolJob,
    ToolJobState,
    TranscriptItemKind,
    WorkflowExecution,
    WorkflowExecutionState,
    replace_record,
)

Record = (
    ChatThread
    | ChatMessage
    | Attachment
    | Investigation
    | WorkflowExecution
    | ToolJob
    | SubAgentRecord
    | AgentInvocation
)
RecordType = type[Record]
RecordT = TypeVar("RecordT", bound=Record)

_ID_FIELDS: Mapping[RecordType, str] = {
    ChatThread: "chat_id",
    ChatMessage: "message_id",
    Attachment: "attachment_id",
    Investigation: "investigation_id",
    WorkflowExecution: "execution_id",
    ToolJob: "tool_job_id",
    SubAgentRecord: "sub_agent_id",
    AgentInvocation: "invocation_id",
}

_RECORD_TYPES: tuple[RecordType, ...] = (
    ChatThread,
    ChatMessage,
    Attachment,
    Investigation,
    WorkflowExecution,
    ToolJob,
    SubAgentRecord,
    AgentInvocation,
)

_SKIPPED_FIELDS = frozenset({"schema_version", "content_hash"})
_CONVERSATION_LOCKS: dict[tuple[str, str, str], RLock] = {}
_LOCK_REGISTRY_GUARD = Lock()


def conversation_lock(store: str, workspace_id: str, root: str) -> RLock:
    """Share a single-process write/delete guard across adapter instances."""
    with _LOCK_REGISTRY_GUARD:
        return _CONVERSATION_LOCKS.setdefault((store, workspace_id, root), RLock())


def ensure_ownership_unchanged(previous: Record | None, record: Record) -> None:
    """Reject moving a persisted identity to a different conversation."""
    if isinstance(previous, ConversationRecord) and isinstance(
        record, ConversationRecord
    ):
        for name in (
            "chat_id",
            "conversation_id",
            "root_conversation_id",
            "parent_conversation_id",
        ):
            if getattr(previous, name) != getattr(record, name):
                raise ChatContractError("conversation ownership is immutable")


def _record_id(record: Record) -> str:
    """Return the stable identity for a supported record."""
    id_name = _ID_FIELDS.get(type(record))
    if id_name is not None:
        value = getattr(record, id_name)
        if isinstance(value, str):
            return value
    raise TypeError(f"record has no stable ID: {type(record).__name__}")


def _ensure_supported(record_type: RecordType) -> None:
    if record_type not in _ID_FIELDS:
        raise TypeError(f"unsupported chat record type: {record_type.__name__}")


def _payload(record: Record) -> dict[str, object]:
    """Serialize a record to its durable JSON payload."""
    detached = record.as_dict()
    return {key: value for key, value in detached.items() if key not in _SKIPPED_FIELDS}


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _datetime(value)


def _optional_text(value: object) -> str | None:
    """Return a non-empty string payload or ``None`` for absent/blank values."""
    return None if value is None else str(value)


def _ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _int(value: object, default: int = 0) -> int:
    """Coerce a JSON scalar to ``int`` with an explicit, validated fallback."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError("expected an integer payload")


def _bool(value: object, default: bool = False) -> bool:
    """Coerce a JSON scalar to ``bool`` with an explicit fallback."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    raise TypeError("expected a boolean payload")


def _mapping(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("expected a mapping payload")
    return {str(key): item for key, item in value.items()}


def _links(value: object) -> RecordLinks:
    data = value if isinstance(value, Mapping) else {}
    return RecordLinks(
        chat_ids=_ids(data.get("chat_ids")),
        investigation_ids=_ids(data.get("investigation_ids")),
        strategy_ids=_ids(data.get("strategy_ids")),
        experiment_ids=_ids(data.get("experiment_ids")),
        workflow_execution_ids=_ids(data.get("workflow_execution_ids")),
        tool_job_ids=_ids(data.get("tool_job_ids")),
    )


def _from_payload(record_type: RecordType, data: Mapping[str, object]) -> Record:
    """Reconstruct one record from its durable JSON payload."""
    _ensure_supported(record_type)
    ownership: dict[str, Any] = {
        name: data[name]
        for name in (
            "conversation_id",
            "root_conversation_id",
            "parent_conversation_id",
        )
        if name in data
    }
    if record_type is ChatThread:
        return ChatThread(
            **ownership,
            workspace_id=str(data["workspace_id"]),
            created_at=_datetime(data["created_at"]),
            created_by=str(data["created_by"]),
            chat_id=str(data["chat_id"]),
            title=str(data["title"]),
            owner_actor_id=str(data["owner_actor_id"]),
            state=ChatState(str(data["state"])),
            links=_links(data.get("links")),
            updated_at=_optional_datetime(data.get("updated_at")),
            input_consumed_positions=cast(
                Mapping[str, int], _mapping(data.get("input_consumed_positions")) or {}
            ),
        )
    if record_type is ChatMessage:
        return ChatMessage(
            **ownership,
            workspace_id=str(data["workspace_id"]),
            created_at=_datetime(data["created_at"]),
            created_by=str(data["created_by"]),
            message_id=str(data["message_id"]),
            chat_id=str(data["chat_id"]),
            actor_id=str(data["actor_id"]),
            role=MessageRole(str(data["role"])),
            body=str(data["body"]),
            reasoning=None if data.get("reasoning") is None else str(data["reasoning"]),
            tool_result=_mapping(data.get("tool_result")),
            kind=TranscriptItemKind(str(data.get("kind", TranscriptItemKind.MESSAGE))),
            data=_mapping(data.get("data")),
            state=MessageState(str(data["state"])),
            attachment_ids=_ids(data.get("attachment_ids")),
            investigation_ids=_ids(data.get("investigation_ids")),
            execution_id=None
            if data.get("execution_id") is None
            else str(data["execution_id"]),
            intent_epoch=_int(data.get("intent_epoch")),
            input_position=_int(data.get("input_position")),
            updated_at=_optional_datetime(data.get("updated_at")),
        )
    if record_type is Attachment:
        return Attachment(
            **ownership,
            workspace_id=str(data["workspace_id"]),
            created_at=_datetime(data["created_at"]),
            created_by=str(data["created_by"]),
            attachment_id=str(data["attachment_id"]),
            chat_id=str(data["chat_id"]),
            message_id=str(data["message_id"]),
            filename=str(data["filename"]),
            media_type=str(data["media_type"]),
            size_bytes=_int(data["size_bytes"]),
            state=AttachmentState(str(data["state"])),
            sha256=None if data.get("sha256") is None else str(data["sha256"]),
            content_uri=None
            if data.get("content_uri") is None
            else str(data["content_uri"]),
            error_message=(
                None
                if data.get("error_message") is None
                else str(data["error_message"])
            ),
            updated_at=_optional_datetime(data.get("updated_at")),
        )
    if record_type is Investigation:
        return Investigation(
            workspace_id=str(data["workspace_id"]),
            created_at=_datetime(data["created_at"]),
            created_by=str(data["created_by"]),
            investigation_id=str(data["investigation_id"]),
            title=str(data["title"]),
            objective=str(data["objective"]),
            owner_actor_id=str(data["owner_actor_id"]),
            state=InvestigationState(str(data["state"])),
            links=_links(data.get("links")),
            updated_at=_optional_datetime(data.get("updated_at")),
        )
    if record_type is WorkflowExecution:
        return WorkflowExecution(
            **ownership,
            workspace_id=str(data["workspace_id"]),
            created_at=_datetime(data["created_at"]),
            created_by=str(data["created_by"]),
            execution_id=str(data["execution_id"]),
            chat_id=str(data["chat_id"]),
            trigger_message_id=str(data["trigger_message_id"]),
            workflow_id=str(data["workflow_id"]),
            workflow_version=str(data["workflow_version"]),
            requested_by=str(data["requested_by"]),
            investigation_id=(
                None
                if data.get("investigation_id") is None
                else str(data["investigation_id"])
            ),
            strategy_ids=_ids(data.get("strategy_ids")),
            experiment_ids=_ids(data.get("experiment_ids")),
            tool_job_ids=_ids(data.get("tool_job_ids")),
            state=WorkflowExecutionState(str(data["state"])),
            phase=None if data.get("phase") is None else str(data["phase"]),
            intent_epoch=_int(data.get("intent_epoch")),
            steering_message_ids=_ids(data.get("steering_message_ids")),
            updated_at=_optional_datetime(data.get("updated_at")),
            finished_at=_optional_datetime(data.get("finished_at")),
        )
    if record_type is SubAgentRecord:
        return SubAgentRecord(
            **ownership,
            workspace_id=str(data["workspace_id"]),
            created_at=_datetime(data["created_at"]),
            created_by=str(data["created_by"]),
            sub_agent_id=str(data["sub_agent_id"]),
            chat_id=str(data["chat_id"]),
            role=str(data["role"]),
            parent_sub_agent_id=_optional_text(data.get("parent_sub_agent_id")),
            spawned_by_invocation_id=_optional_text(
                data.get("spawned_by_invocation_id")
            ),
            workflow_execution_id=_optional_text(data.get("workflow_execution_id")),
            phase_attempt_id=_optional_text(data.get("phase_attempt_id")),
            phase=_optional_text(data.get("phase")),
            spec_version=str(data.get("spec_version", "1.0.0")),
            kind=SubAgentKind(str(data.get("kind", SubAgentKind.CONVERSATION))),
            completion_mode=CompletionMode(
                str(
                    data.get(
                        "completion_mode",
                        (
                            CompletionMode.COMPLETE_ON_TEXT
                            if data.get("kind") == SubAgentKind.ONE_SHOT
                            and not data.get("phase")
                            else CompletionMode.EXPLICIT_COMPLETION
                        ),
                    )
                )
            ),
            returns_to_sub_agent_id=_optional_text(
                data.get("returns_to_sub_agent_id", data.get("parent_sub_agent_id"))
            ),
            phase_executor=_bool(
                data.get("phase_executor"),
                default=bool(
                    data.get("phase")
                    and data.get("workflow_execution_id")
                    and data.get("phase_attempt_id")
                    and data.get("parent_sub_agent_id")
                ),
            ),
            allowed_tools=_ids(data.get("allowed_tools")),
            status=SubAgentStatus(str(data.get("status", SubAgentStatus.PENDING))),
            accepts_input=_bool(data.get("accepts_input")),
            input_contract_ref=_optional_text(data.get("input_contract_ref")),
            completion_contract_ref=_optional_text(data.get("completion_contract_ref")),
            result_ref=_optional_text(data.get("result_ref")),
            result=_mapping(data.get("result")),
            delegated_budget=_int(data.get("delegated_budget")),
            updated_at=_optional_datetime(data.get("updated_at")),
        )
    if record_type is AgentInvocation:
        kind = AgentInvocationKind(str(data.get("kind", AgentInvocationKind.MODEL)))
        request_messages = data.get("request_messages", ())
        # References and shipped full-message audits are both opaque evidence.
        # Do not synthesize message contents or reconstruct the provider request.
        if not isinstance(request_messages, list | tuple) or any(
            not isinstance(message, Mapping) for message in request_messages
        ):
            raise TypeError("request_messages must be a sequence of mappings")
        return AgentInvocation(
            **ownership,
            workspace_id=str(data["workspace_id"]),
            created_at=_datetime(data["created_at"]),
            created_by=str(data["created_by"]),
            invocation_id=str(data["invocation_id"]),
            chat_id=str(data["chat_id"]),
            kind=kind,
            state=AgentInvocationState(
                str(data.get("state", AgentInvocationState.REQUESTED))
            ),
            sub_agent_id=_optional_text(data.get("sub_agent_id")),
            parent_invocation_id=_optional_text(data.get("parent_invocation_id")),
            workflow_execution_id=_optional_text(data.get("workflow_execution_id")),
            phase_attempt_id=_optional_text(data.get("phase_attempt_id")),
            role=_optional_text(data.get("role")),
            model_profile=_optional_text(data.get("model_profile")),
            tool_name=_optional_text(data.get("tool_name")),
            policy_hash=_optional_text(data.get("policy_hash")),
            input_hash=_optional_text(data.get("input_hash")),
            request_messages=cast(
                tuple[Mapping[str, object], ...], tuple(request_messages)
            ),
            provider_request=_mapping(data.get("provider_request")),
            request_message_id=_optional_text(data.get("request_message_id")),
            tool_request_id=_optional_text(data.get("tool_request_id")),
            output_hash=_optional_text(data.get("output_hash")),
            idempotency_key=_optional_text(data.get("idempotency_key")),
            intent_epoch=_int(data.get("intent_epoch")),
            failure=_mapping(data.get("failure")),
            updated_at=_optional_datetime(data.get("updated_at")),
        )
    return ToolJob(
        **ownership,
        workspace_id=str(data["workspace_id"]),
        created_at=_datetime(data["created_at"]),
        created_by=str(data["created_by"]),
        tool_job_id=str(data["tool_job_id"]),
        execution_id=str(data["execution_id"]),
        chat_id=str(data["chat_id"]),
        correlation_id=str(data["correlation_id"]),
        tool_name=str(data["tool_name"]),
        submitted_by=str(data["submitted_by"]),
        investigation_id=(
            None
            if data.get("investigation_id") is None
            else str(data["investigation_id"])
        ),
        strategy_ids=_ids(data.get("strategy_ids")),
        experiment_ids=_ids(data.get("experiment_ids")),
        intent_epoch=_int(data.get("intent_epoch")),
        state=ToolJobState(str(data["state"])),
        result=_mapping(data.get("result")),
        error_message=None
        if data.get("error_message") is None
        else str(data["error_message"]),
        late_result=_bool(data.get("late_result")),
        updated_at=_optional_datetime(data.get("updated_at")),
    )


def _atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` atomically via a same-directory temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=".chat-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class FileChatPersistence:
    """Durable JSON-document persistence under one workspace root.

    Satisfies the structural :class:`~thesis.chat.service.ChatPersistence`
    protocol, so it drops into :class:`~thesis.chat.service.ChatService` in place
    of :class:`~thesis.chat.service.InMemoryChatPersistence`.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """Return the durable root directory."""
        return self._root

    def _type_dir(self, record_type: RecordType) -> Path:
        _ensure_supported(record_type)
        return self._root / record_type.__name__

    def _path(self, record_type: RecordType, workspace_id: str, record_id: str) -> Path:
        digest = hashlib.sha256(f"{workspace_id}\x00{record_id}".encode()).hexdigest()
        return self._type_dir(record_type) / f"{digest}.json"

    def save(self, record: RecordT) -> RecordT:
        if isinstance(record, ConversationRecord):
            with conversation_lock(
                str(self._root.resolve()), record.workspace_id, record.chat_id
            ):
                root = self.get(ChatThread, record.workspace_id, record.chat_id)
                if root is not None and root.state is ChatState.DELETED:
                    raise ChatContractError("conversation has been deleted")
                ensure_ownership_unchanged(
                    self.get(type(record), record.workspace_id, _record_id(record)),
                    record,
                )
                return self._save(record)
        return self._save(record)

    def _save(self, record: RecordT) -> RecordT:
        record_type = cast(RecordType, type(record))
        _ensure_supported(record_type)
        record_id = _record_id(record)
        path = self._path(record_type, record.workspace_id, record_id)
        document = json.dumps(
            {"record": _payload(record)},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        _atomic_write(path, document)
        return record

    def get(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> RecordT | None:
        typed = cast(RecordType, record_type)
        _ensure_supported(typed)
        path = self._path(typed, workspace_id, record_id)
        if not path.is_file():
            return None
        record = self._read(path)
        if record.workspace_id != workspace_id or _record_id(record) != record_id:
            return None
        return cast(RecordT, record)

    def list(
        self, record_type: type[RecordT], workspace_id: str
    ) -> tuple[RecordT, ...]:
        typed = cast(RecordType, record_type)
        _ensure_supported(typed)
        directory = self._type_dir(typed)
        if not directory.is_dir():
            return ()
        records = [self._read(path) for path in sorted(directory.glob("*.json"))]
        selected = [
            record
            for record in records
            if record.workspace_id == workspace_id
            and not (
                isinstance(record, ChatThread) and record.state is ChatState.DELETED
            )
        ]
        selected.sort(key=lambda record: (record.created_at, _record_id(record)))
        return cast(tuple[RecordT, ...], tuple(selected))

    def list_conversation(
        self,
        record_type: type[RecordT],
        workspace_id: str,
        root_conversation_id: str,
        *,
        conversation_id: str | None = None,
    ) -> tuple[RecordT, ...]:
        if not issubclass(record_type, ConversationRecord):
            raise TypeError("record type is not conversation-owned")
        return tuple(
            record
            for record in self.list(record_type, workspace_id)
            if isinstance(record, ConversationRecord)
            and record.root_conversation_id == root_conversation_id
            and (conversation_id is None or record.conversation_id == conversation_id)
        )

    def mark_conversation_deleted(
        self,
        workspace_id: str,
        chat_id: str,
        deleted_at: datetime,
    ) -> None:
        with conversation_lock(str(self._root.resolve()), workspace_id, chat_id):
            root = self.get(ChatThread, workspace_id, chat_id)
            if root is not None and root.state is not ChatState.DELETED:
                self._save(
                    replace_record(root, state=ChatState.DELETED, updated_at=deleted_at)
                )

    def list_deleted_conversations(self, workspace_id: str) -> tuple[ChatThread, ...]:
        directory = self._type_dir(ChatThread)
        if not directory.is_dir():
            return ()
        return tuple(
            record
            for path in directory.glob("*.json")
            if isinstance((record := self._read(path)), ChatThread)
            and record.workspace_id == workspace_id
            and record.state is ChatState.DELETED
        )

    def purge_conversation(
        self,
        workspace_id: str,
        chat_id: str,
        limit: int = 100,
    ) -> bool:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with conversation_lock(str(self._root.resolve()), workspace_id, chat_id):
            root = self.get(ChatThread, workspace_id, chat_id)
            if root is None or root.state is not ChatState.DELETED:
                raise ChatContractError("purge requires a deleted root conversation")
            remaining = limit
            for record_type in _RECORD_TYPES:
                if record_type is ChatThread or not issubclass(
                    record_type, ConversationRecord
                ):
                    continue
                for record in self.list_conversation(
                    record_type, workspace_id, chat_id
                ):
                    if remaining == 0:
                        return False
                    self.delete(record_type, workspace_id, _record_id(record))
                    remaining -= 1
            return True

    def delete(
        self, record_type: RecordType, workspace_id: str, record_id: str
    ) -> None:
        if record_type is ChatThread:
            with conversation_lock(str(self._root.resolve()), workspace_id, record_id):
                root = self.get(ChatThread, workspace_id, record_id)
                if root is not None and root.state is ChatState.DELETED:
                    raise ChatContractError("a conversation tombstone must be retained")
                self._delete(record_type, workspace_id, record_id)
            return
        self._delete(record_type, workspace_id, record_id)

    def _delete(
        self, record_type: RecordType, workspace_id: str, record_id: str
    ) -> None:
        path = self._path(record_type, workspace_id, record_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return

    def _read(self, path: Path) -> Record:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping) or "record" not in document:
            raise ValueError(f"malformed chat persistence document: {path.name}")
        payload = document["record"]
        if not isinstance(payload, Mapping):
            raise ValueError(f"malformed chat persistence record: {path.name}")
        record_type = _type_from_name(path.parent.name)
        return _from_payload(record_type, payload)


def _type_from_name(name: str) -> RecordType:
    for record_type in _RECORD_TYPES:
        if record_type.__name__ == name:
            return record_type
    raise TypeError(f"unknown chat record type directory: {name}")


def records() -> Iterable[RecordType]:
    """Return the supported durable record types in stable order."""
    return _RECORD_TYPES


def record_identity(record: Record) -> str:
    """Return one record's stable identity (public for other stores)."""
    return _record_id(record)


def ensure_supported_record_type(record_type: RecordType) -> None:
    """Reject a record type that no durable chat store can persist."""
    _ensure_supported(record_type)


def record_payload(record: Record) -> dict[str, object]:
    """Serialize a record to the shared durable JSON payload."""
    return _payload(record)


def record_from_payload(
    record_type: RecordType, payload: Mapping[str, object]
) -> Record:
    """Reconstruct a record from the shared durable JSON payload."""
    return _from_payload(record_type, payload)


__all__ = [
    "FileChatPersistence",
    "ensure_supported_record_type",
    "record_from_payload",
    "record_identity",
    "record_payload",
    "records",
]
