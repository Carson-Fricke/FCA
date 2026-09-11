"""Workspace-scoped contracts for Thesis chat and autonomous work.

The records in this module are transport and persistence contracts.  They do
not import CLEO, FastAPI, a model provider, or a storage implementation.
Mutable lifecycle operations create a new record snapshot rather than mutating
an existing record in place.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, datetime
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Any, cast

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")


class ChatContractError(ValueError):
    """Base error for invalid chat, investigation, or execution records."""


class InvalidLifecycleTransition(ChatContractError):
    """A record cannot move directly between the requested lifecycle states."""


class RecordNotFound(LookupError):
    """A requested workspace record does not exist."""


class CrossWorkspaceLinkError(ChatContractError):
    """A record attempted to link to an entity in another workspace."""


class ChatState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class MessageState(StrEnum):
    DRAFT = "draft"
    ACCEPTED = "accepted"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETED = "deleted"


class AttachmentState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    SCANNING = "scanning"
    AVAILABLE = "available"
    REJECTED = "rejected"
    DELETED = "deleted"


class InvestigationState(StrEnum):
    OPEN = "open"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class WorkflowExecutionState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolJobState(StrEnum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _validate_id(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or _ID_PATTERN.fullmatch(value) is None
        or value.startswith(("/", "\\"))
        or ".." in value
        or "//" in value
    ):
        raise ChatContractError(f"{name} must be a non-empty safe identifier")


def _validate_workspace_id(value: str) -> None:
    if not isinstance(value, str) or _WORKSPACE_ID_PATTERN.fullmatch(value) is None:
        raise ChatContractError("workspace_id must be a safe workspace identifier")


def _validate_text(value: str, name: str, *, max_length: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ChatContractError(f"{name} must be non-empty")
    if len(value) > max_length or any(ord(character) < 32 for character in value):
        raise ChatContractError(f"{name} contains unsupported characters")


def _validate_body(value: str, name: str, *, max_length: int) -> str:
    """Validate a free-text message body, which may span multiple lines.

    Message bodies are multi-line by nature, so newlines, carriage returns,
    and tabs are permitted; every other control character is rejected. Line
    endings are normalized to ``\\n``.
    """
    if not isinstance(value, str) or not value.strip():
        raise ChatContractError(f"{name} must be non-empty")
    if len(value) > max_length or any(
        ord(character) < 32 and character not in "\n\t\r" for character in value
    ):
        raise ChatContractError(f"{name} contains unsupported characters")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _validate_timestamp(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ChatContractError(f"{name} must be timezone-aware")


def _normalise_ids(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ChatContractError(f"{name} values must be a collection of identifiers")
    if not isinstance(values, tuple):
        values = tuple(values)
    result = tuple(dict.fromkeys(values))
    for value in result:
        _validate_id(value, name)
    return result


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if is_dataclass(value):
        return {
            item.name: _json_value(getattr(value, item.name))
            for item in fields(cast(Any, value))
        }
    return value


def _canonical_record(record: WorkspaceRecord) -> str:
    values: dict[str, object] = {}
    for item in fields(record):
        if item.name == "content_hash":
            continue
        values[item.name] = _json_value(getattr(record, item.name))
    return json.dumps(values, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _record_hash(record: WorkspaceRecord) -> str:
    return hashlib.sha256(_canonical_record(record).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceRecord:
    """Common immutable identity and provenance for all durable records."""

    workspace_id: str
    created_at: datetime
    created_by: str
    schema_version: int = field(default=1, init=False)
    content_hash: str = field(default="", init=False)

    def __post_init__(self) -> None:
        _validate_workspace_id(self.workspace_id)
        _validate_id(self.created_by, "created_by")
        _validate_timestamp(self.created_at, "created_at")
        if self.schema_version != 1:
            raise ChatContractError("unsupported record schema version")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        object.__setattr__(self, "content_hash", _record_hash(self))

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-compatible detached representation for UI transport."""
        return _as_dict(self)


@dataclass(frozen=True, slots=True)
class RecordLinks:
    """Explicit links shared by chat, investigation, and execution records."""

    chat_ids: tuple[str, ...] = ()
    investigation_ids: tuple[str, ...] = ()
    strategy_ids: tuple[str, ...] = ()
    experiment_ids: tuple[str, ...] = ()
    workflow_execution_ids: tuple[str, ...] = ()
    tool_job_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "chat_ids",
            "investigation_ids",
            "strategy_ids",
            "experiment_ids",
            "workflow_execution_ids",
            "tool_job_ids",
        ):
            object.__setattr__(self, name, _normalise_ids(getattr(self, name), name))

    def add(self, **updates: tuple[str, ...]) -> RecordLinks:
        """Return links with values appended and duplicate IDs removed."""
        values = {
            name: getattr(self, name)
            for name in (
                "chat_ids",
                "investigation_ids",
                "strategy_ids",
                "experiment_ids",
                "workflow_execution_ids",
                "tool_job_ids",
            )
        }
        for name, additions in updates.items():
            if name not in values:
                raise ChatContractError(f"unknown link collection: {name}")
            if isinstance(additions, str):
                raise ChatContractError(f"{name} values must be a collection")
            values[name] = (*values[name], *tuple(additions))
        return RecordLinks(**values)


@dataclass(frozen=True, slots=True)
class ChatThread(WorkspaceRecord):
    """A persistent conversational thread within exactly one workspace."""

    chat_id: str
    title: str
    owner_actor_id: str
    state: ChatState = ChatState.ACTIVE
    links: RecordLinks = field(default_factory=RecordLinks)
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_id(self.chat_id, "chat_id")
        _validate_text(self.title, "title", max_length=256)
        _validate_id(self.owner_actor_id, "owner_actor_id")
        if self.updated_at is not None:
            _validate_timestamp(self.updated_at, "updated_at")
        WorkspaceRecord.__post_init__(self)

    @property
    def investigation_ids(self) -> tuple[str, ...]:
        return self.links.investigation_ids

    @property
    def strategy_ids(self) -> tuple[str, ...]:
        return self.links.strategy_ids

    @property
    def experiment_ids(self) -> tuple[str, ...]:
        return self.links.experiment_ids

    @property
    def tool_job_ids(self) -> tuple[str, ...]:
        return self.links.tool_job_ids

    @property
    def execution_ids(self) -> tuple[str, ...]:
        return self.links.workflow_execution_ids


@dataclass(frozen=True, slots=True)
class ChatMessage(WorkspaceRecord):
    """One durable user, assistant, system, or tool message."""

    message_id: str
    chat_id: str
    actor_id: str
    role: MessageRole
    body: str
    state: MessageState = MessageState.ACCEPTED
    attachment_ids: tuple[str, ...] = ()
    investigation_ids: tuple[str, ...] = ()
    execution_id: str | None = None
    intent_epoch: int = 0
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_id(self.message_id, "message_id")
        _validate_id(self.chat_id, "chat_id")
        _validate_id(self.actor_id, "actor_id")
        object.__setattr__(
            self, "body", _validate_body(self.body, "body", max_length=32_768)
        )
        if self.intent_epoch < 0:
            raise ChatContractError("intent_epoch must not be negative")
        object.__setattr__(
            self, "attachment_ids", _normalise_ids(self.attachment_ids, "attachment_id")
        )
        object.__setattr__(
            self,
            "investigation_ids",
            _normalise_ids(self.investigation_ids, "investigation_id"),
        )
        if self.execution_id is not None:
            _validate_id(self.execution_id, "execution_id")
        if self.updated_at is not None:
            _validate_timestamp(self.updated_at, "updated_at")
        WorkspaceRecord.__post_init__(self)


@dataclass(frozen=True, slots=True)
class Attachment(WorkspaceRecord):
    """Metadata for an untrusted upload; bytes remain behind an injected port."""

    attachment_id: str
    chat_id: str
    message_id: str
    filename: str
    media_type: str
    size_bytes: int
    state: AttachmentState = AttachmentState.PENDING
    sha256: str | None = None
    content_uri: str | None = None
    error_message: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_id(self.attachment_id, "attachment_id")
        _validate_id(self.chat_id, "chat_id")
        _validate_id(self.message_id, "message_id")
        _validate_text(self.filename, "filename", max_length=512)
        _validate_text(self.media_type, "media_type", max_length=255)
        if self.size_bytes < 0:
            raise ChatContractError("size_bytes must not be negative")
        if self.sha256 is not None and _HASH_PATTERN.fullmatch(self.sha256) is None:
            raise ChatContractError("sha256 must be a lowercase SHA-256 hash")
        if self.content_uri is not None:
            _validate_text(self.content_uri, "content_uri", max_length=2048)
        if self.error_message is not None:
            _validate_text(self.error_message, "error_message", max_length=2048)
        if self.updated_at is not None:
            _validate_timestamp(self.updated_at, "updated_at")
        WorkspaceRecord.__post_init__(self)


@dataclass(frozen=True, slots=True)
class Investigation(WorkspaceRecord):
    """A durable research objective spanning chats and research artifacts."""

    investigation_id: str
    title: str
    objective: str
    owner_actor_id: str
    state: InvestigationState = InvestigationState.OPEN
    links: RecordLinks = field(default_factory=RecordLinks)
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_id(self.investigation_id, "investigation_id")
        _validate_text(self.title, "title", max_length=256)
        _validate_text(self.objective, "objective", max_length=8_192)
        _validate_id(self.owner_actor_id, "owner_actor_id")
        if self.updated_at is not None:
            _validate_timestamp(self.updated_at, "updated_at")
        WorkspaceRecord.__post_init__(self)

    @property
    def chat_ids(self) -> tuple[str, ...]:
        return self.links.chat_ids

    @property
    def strategy_ids(self) -> tuple[str, ...]:
        return self.links.strategy_ids

    @property
    def experiment_ids(self) -> tuple[str, ...]:
        return self.links.experiment_ids

    @property
    def execution_ids(self) -> tuple[str, ...]:
        return self.links.workflow_execution_ids

    @property
    def tool_job_ids(self) -> tuple[str, ...]:
        return self.links.tool_job_ids


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """Versioned workflow metadata selected by a caller or registry adapter."""

    workflow_id: str
    version: str
    title: str
    phases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_id(self.workflow_id, "workflow_id")
        if _VERSION_PATTERN.fullmatch(self.version) is None:
            raise ChatContractError("workflow version must be semantic version text")
        _validate_text(self.title, "workflow title", max_length=256)
        object.__setattr__(self, "phases", _normalise_ids(self.phases, "phase"))

    def as_dict(self) -> dict[str, object]:
        return _as_dict(self)


@dataclass(frozen=True, slots=True)
class WorkflowExecution(WorkspaceRecord):
    """Durable workflow state independent of any browser connection."""

    execution_id: str
    chat_id: str
    trigger_message_id: str
    workflow_id: str
    workflow_version: str
    requested_by: str
    investigation_id: str | None = None
    strategy_ids: tuple[str, ...] = ()
    experiment_ids: tuple[str, ...] = ()
    tool_job_ids: tuple[str, ...] = ()
    state: WorkflowExecutionState = WorkflowExecutionState.QUEUED
    phase: str | None = None
    intent_epoch: int = 0
    steering_message_ids: tuple[str, ...] = ()
    updated_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in (
            "execution_id",
            "chat_id",
            "trigger_message_id",
            "workflow_id",
            "requested_by",
        ):
            _validate_id(getattr(self, name), name)
        _validate_text(self.workflow_version, "workflow_version", max_length=64)
        if self.investigation_id is not None:
            _validate_id(self.investigation_id, "investigation_id")
        if self.phase is not None:
            _validate_text(self.phase, "phase", max_length=128)
        if self.intent_epoch < 0:
            raise ChatContractError("intent_epoch must not be negative")
        object.__setattr__(
            self, "strategy_ids", _normalise_ids(self.strategy_ids, "strategy_id")
        )
        object.__setattr__(
            self,
            "experiment_ids",
            _normalise_ids(self.experiment_ids, "experiment_id"),
        )
        object.__setattr__(
            self, "tool_job_ids", _normalise_ids(self.tool_job_ids, "tool_job_id")
        )
        object.__setattr__(
            self,
            "steering_message_ids",
            _normalise_ids(self.steering_message_ids, "message_id"),
        )
        for name in ("updated_at", "finished_at"):
            value = getattr(self, name)
            if value is not None:
                _validate_timestamp(value, name)
        WorkspaceRecord.__post_init__(self)


@dataclass(frozen=True, slots=True)
class ToolJob(WorkspaceRecord):
    """An externally submitted tool operation whose result is never discarded."""

    tool_job_id: str
    execution_id: str
    chat_id: str
    correlation_id: str
    tool_name: str
    submitted_by: str
    investigation_id: str | None = None
    strategy_ids: tuple[str, ...] = ()
    experiment_ids: tuple[str, ...] = ()
    intent_epoch: int = 0
    state: ToolJobState = ToolJobState.SUBMITTED
    result: Mapping[str, object] | None = None
    error_message: str | None = None
    late_result: bool = False
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in (
            "tool_job_id",
            "execution_id",
            "chat_id",
            "correlation_id",
            "tool_name",
            "submitted_by",
        ):
            _validate_id(getattr(self, name), name)
        if self.investigation_id is not None:
            _validate_id(self.investigation_id, "investigation_id")
        if self.intent_epoch < 0:
            raise ChatContractError("intent_epoch must not be negative")
        object.__setattr__(
            self, "strategy_ids", _normalise_ids(self.strategy_ids, "strategy_id")
        )
        object.__setattr__(
            self,
            "experiment_ids",
            _normalise_ids(self.experiment_ids, "experiment_id"),
        )
        if self.result is not None:
            object.__setattr__(self, "result", MappingProxyType(dict(self.result)))
        if self.error_message is not None:
            _validate_text(self.error_message, "error_message", max_length=2048)
        if self.updated_at is not None:
            _validate_timestamp(self.updated_at, "updated_at")
        WorkspaceRecord.__post_init__(self)


def _as_dict(record: object) -> dict[str, object]:
    """Serialize dataclasses without exposing a mutable internal mapping."""
    if not is_dataclass(record):
        raise TypeError("record must be a dataclass")
    result: dict[str, object] = {}
    for item in fields(cast(Any, record)):
        value = getattr(record, item.name)
        if isinstance(value, Enum):
            value = value.value
        elif isinstance(value, datetime):
            value = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        elif isinstance(value, Mapping):
            value = {
                str(key): _as_json(item_value) for key, item_value in value.items()
            }
        elif isinstance(value, tuple):
            value = [_as_json(item) for item in value]
        elif is_dataclass(value):
            value = _as_dict(value)
        result[item.name] = value
    return result


def _as_json(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _as_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_as_json(item) for item in value]
    if is_dataclass(value):
        return _as_dict(value)
    return value


def replace_record[T: WorkspaceRecord](record: T, **changes: Any) -> T:
    """Replace a lifecycle snapshot and recompute its content hash."""
    changes.pop("content_hash", None)
    return replace(record, **changes)


__all__ = [
    "Attachment",
    "AttachmentState",
    "ChatContractError",
    "ChatMessage",
    "ChatState",
    "ChatThread",
    "CrossWorkspaceLinkError",
    "InvalidLifecycleTransition",
    "Investigation",
    "InvestigationState",
    "MessageRole",
    "MessageState",
    "RecordLinks",
    "RecordNotFound",
    "ToolJob",
    "ToolJobState",
    "WorkflowDefinition",
    "WorkflowExecution",
    "WorkflowExecutionState",
    "WorkspaceRecord",
    "replace_record",
]
