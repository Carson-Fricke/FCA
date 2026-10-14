"""Workspace-scoped contracts for Thesis chat and autonomous work.

The records in this module are transport and persistence contracts.  They do
not import FastAPI, a model provider, or a storage implementation.
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
from typing import Any, Literal, cast

ExecutionCertainty = Literal["not_started", "observed_failure", "unknown_outcome"]

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_MAX_REASONING = 32_768


class ChatContractError(ValueError):
    """Base error for invalid conversation or execution records."""


class InvalidLifecycleTransition(ChatContractError):
    """A record cannot move directly between the requested lifecycle states."""


class RecordNotFound(LookupError):
    """A requested workspace record does not exist."""


class CrossWorkspaceLinkError(ChatContractError):
    """A record attempted to link to an entity in another workspace."""


class ToolJobConflictError(ChatContractError):
    """A tool job id is already registered as a different operation."""


class ChatState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


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


class TranscriptItemKind(StrEnum):
    """The discriminator carried by one conversation item."""

    MESSAGE = "message"
    TOOL = "tool"
    SUB_AGENT = "sub_agent"
    ARTIFACT = "artifact"


class AttachmentState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    SCANNING = "scanning"
    AVAILABLE = "available"
    REJECTED = "rejected"
    DELETED = "deleted"


class WorkflowExecutionState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
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


class SubAgentKind(StrEnum):
    """The two agent-backed phase kinds; deterministic work is never a sub-agent."""

    CONVERSATION = "conversation"
    ONE_SHOT = "one_shot"


class CompletionMode(StrEnum):
    COMPLETE_ON_TEXT = "complete_on_text"
    EXPLICIT_COMPLETION = "explicit_completion"


#: Durable role of the workflow-node record that owns one execution's phase
#: agents.  The node is a ``SubAgentRecord``, so the existing depth, children,
#: and budget guards apply to the phase agents spawned beneath it.
WORKFLOW_NODE_ROLE = "workflow-node"

#: Roles whose records anchor delivered data but are never run as
#: conversations: delivering at such a record's anchor is durable-only and
#: schedules nothing, because the reacting party already ran.
RECORD_ANCHOR_ROLES: frozenset[str] = frozenset({WORKFLOW_NODE_ROLE})


class SubAgentStatus(StrEnum):
    """Lifecycle status projected for one durable sub-agent."""

    PENDING = "pending"
    RUNNING = "running"
    NEEDS_INPUT = "needs_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentInvocationKind(StrEnum):
    """First-class invocation discriminator for model and tool calls."""

    MODEL = "model"
    TOOL = "tool"


class AgentInvocationState(StrEnum):
    """Durable lifecycle of one model or tool invocation."""

    REQUESTED = "requested"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


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


def normalise_reasoning(value: str | None) -> str | None:
    """Normalize an optional reasoning channel, treating blanks as absent.

    Reasoning is a free-text, multi-line model side channel that is never part
    of the message ``body``.  Provider output is normalized rather than
    rejected: stray control characters are dropped, line endings collapse to
    ``\\n``, and an over-long trace is bounded to the record maximum, so a
    display detail can never fail message persistence.  Empty or
    whitespace-only values collapse to ``None`` so older records and
    non-reasoning messages stay byte-identical.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ChatContractError("reasoning must be text")
    cleaned = "".join(
        character
        for character in value.replace("\r\n", "\n").replace("\r", "\n")
        if ord(character) >= 32 or character in "\n\t"
    )
    if not cleaned.strip():
        return None
    return cleaned[:_MAX_REASONING]


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


def normalise_references(
    values: Mapping[str, tuple[str, ...]],
) -> Mapping[str, tuple[str, ...]]:
    """Freeze namespaced application references; the host does not interpret them."""
    if not isinstance(values, Mapping):
        raise ChatContractError("references must be a mapping")
    result = {}
    for namespace, identifiers in values.items():
        _validate_id(namespace, "reference namespace")
        result[namespace] = _normalise_ids(identifiers, "reference identifier")
    return MappingProxyType(result)


def merge_references(
    *values: Mapping[str, tuple[str, ...]],
) -> Mapping[str, tuple[str, ...]]:
    """Merge references without losing the stable order within a namespace."""
    result: dict[str, tuple[str, ...]] = {}
    for value in values:
        for namespace, ids in normalise_references(value).items():
            result[namespace] = tuple(dict.fromkeys((*result.get(namespace, ()), *ids)))
    return normalise_references(result)


def _normalise_attribution(values: Mapping[str, object]) -> Mapping[str, object]:
    """Validate and freeze one JSON-shaped operation attribution mapping."""
    if not isinstance(values, Mapping):
        raise ChatContractError("attribution must be a mapping")
    result: dict[str, object] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            raise ChatContractError("attribution keys must be non-empty strings")
        if not isinstance(value, str | int | float | bool | None):
            raise ChatContractError("attribution values must be JSON scalars")
        result[key] = value
    return MappingProxyType(result)


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


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationRecord(WorkspaceRecord):
    """Explicit immutable ownership, supplied by the creating application."""

    chat_id: str
    conversation_id: str
    root_conversation_id: str
    parent_conversation_id: str | None

    def __post_init__(self) -> None:
        root = self.chat_id
        conversation = self.conversation_id
        parent = self.parent_conversation_id
        if self.root_conversation_id != root:
            raise ChatContractError("root_conversation_id must equal chat_id")
        if not isinstance(conversation, str):
            raise ChatContractError(
                "conversation_id must be a non-empty safe identifier"
            )
        _validate_id(conversation, "conversation_id")
        if isinstance(self, ChatThread) and conversation != root:
            raise ChatContractError("a chat thread must own the root conversation")
        if isinstance(self, SubAgentRecord) and conversation != self.sub_agent_id:
            raise ChatContractError("a sub-agent must own its own conversation")
        if isinstance(self, ChatMessage) and self.data is not None:
            anchor = self.data.get("subAgentId")
            if isinstance(anchor, str) and anchor and anchor != conversation:
                raise ChatContractError("message ownership must match data.subAgentId")
        if conversation == root:
            if parent is not None:
                raise ChatContractError("a root conversation must not have a parent")
        else:
            if not isinstance(parent, str):
                raise ChatContractError(
                    "parent_conversation_id must be a safe identifier"
                )
            _validate_id(parent, "parent_conversation_id")
            if parent == conversation:
                raise ChatContractError("a conversation must not parent itself")
        if isinstance(self, SubAgentRecord) and parent != (
            self.parent_sub_agent_id or root
        ):
            raise ChatContractError("sub-agent ownership must match its direct parent")
        WorkspaceRecord.__post_init__(self)


@dataclass(frozen=True, slots=True)
class RecordLinks:
    """Explicit links shared by conversation and execution records."""

    chat_ids: tuple[str, ...] = ()
    references: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    workflow_execution_ids: tuple[str, ...] = ()
    job_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "references", normalise_references(self.references))
        for name in (
            "chat_ids",
            "workflow_execution_ids",
            "job_ids",
        ):
            object.__setattr__(self, name, _normalise_ids(getattr(self, name), name))

    def as_dict(self) -> dict[str, object]:
        """Detach the opaque references and runtime links for transport."""
        return _as_dict(self)

    def add(
        self,
        *,
        references: Mapping[str, tuple[str, ...]] | None = None,
        **updates: tuple[str, ...],
    ) -> RecordLinks:
        values = {
            name: getattr(self, name)
            for name in ("chat_ids", "workflow_execution_ids", "job_ids")
        }
        for name, additions in updates.items():
            if name not in values or isinstance(additions, str):
                raise ChatContractError(f"invalid link collection: {name}")
            values[name] = (*values[name], *additions)
        return RecordLinks(
            **values, references=merge_references(self.references, references or {})
        )


@dataclass(frozen=True, slots=True)
class ChatThread(ConversationRecord):
    """A persistent conversational thread within exactly one workspace."""

    chat_id: str
    title: str
    owner_actor_id: str
    state: ChatState = ChatState.ACTIVE
    links: RecordLinks = field(default_factory=RecordLinks)
    updated_at: datetime | None = None
    input_consumed_positions: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_id(self.chat_id, "chat_id")
        _validate_text(self.title, "title", max_length=256)
        _validate_id(self.owner_actor_id, "owner_actor_id")
        try:
            object.__setattr__(self, "state", ChatState(self.state))
        except ValueError as error:
            raise ChatContractError("state must be a chat state") from error
        positions = dict(self.input_consumed_positions)
        for anchor, position in positions.items():
            if anchor:
                _validate_id(anchor, "input_consumed_positions anchor")
            if (
                isinstance(position, bool)
                or not isinstance(position, int)
                or position < 0
            ):
                raise ChatContractError(
                    "input consumption positions must be non-negative integers"
                )
        object.__setattr__(
            self, "input_consumed_positions", MappingProxyType(positions)
        )
        if self.updated_at is not None:
            _validate_timestamp(self.updated_at, "updated_at")
        ConversationRecord.__post_init__(self)

    @property
    def job_ids(self) -> tuple[str, ...]:
        return self.links.job_ids

    @property
    def execution_ids(self) -> tuple[str, ...]:
        return self.links.workflow_execution_ids


@dataclass(frozen=True, slots=True)
class ChatMessage(ConversationRecord):
    """One durable user, assistant, system, or tool message."""

    message_id: str
    chat_id: str
    actor_id: str
    role: MessageRole
    body: str
    reasoning: str | None = None
    tool_result: Mapping[str, object] | None = None
    kind: TranscriptItemKind = TranscriptItemKind.MESSAGE
    data: Mapping[str, object] | None = None
    state: MessageState = MessageState.ACCEPTED
    attachment_ids: tuple[str, ...] = ()
    references: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    execution_id: str | None = None
    intent_epoch: int = 0
    input_position: int = 0
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "references", normalise_references(self.references))
        _validate_id(self.message_id, "message_id")
        _validate_id(self.chat_id, "chat_id")
        _validate_id(self.actor_id, "actor_id")
        object.__setattr__(
            self, "body", _validate_body(self.body, "body", max_length=32_768)
        )
        object.__setattr__(self, "reasoning", normalise_reasoning(self.reasoning))
        if not isinstance(self.kind, TranscriptItemKind):
            try:
                object.__setattr__(self, "kind", TranscriptItemKind(self.kind))
            except ValueError as error:
                raise ChatContractError(
                    f"kind must be one of {[item.value for item in TranscriptItemKind]}"
                ) from error
        if self.data is not None:
            if not isinstance(self.data, Mapping):
                raise ChatContractError("data must be a mapping")
            object.__setattr__(self, "data", MappingProxyType(dict(self.data)))
        if self.tool_result is not None:
            if not isinstance(self.tool_result, Mapping):
                raise ChatContractError("tool_result must be a mapping")
            object.__setattr__(
                self, "tool_result", MappingProxyType(dict(self.tool_result))
            )
        if self.intent_epoch < 0:
            raise ChatContractError("intent_epoch must not be negative")
        if self.input_position < 0:
            raise ChatContractError("input_position must not be negative")
        object.__setattr__(
            self, "attachment_ids", _normalise_ids(self.attachment_ids, "attachment_id")
        )
        if self.execution_id is not None:
            _validate_id(self.execution_id, "execution_id")
        if self.updated_at is not None:
            _validate_timestamp(self.updated_at, "updated_at")
        ConversationRecord.__post_init__(self)


@dataclass(frozen=True, slots=True)
class Attachment(ConversationRecord):
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
        ConversationRecord.__post_init__(self)


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
class WorkflowExecution(ConversationRecord):
    """Durable workflow state independent of any browser connection."""

    execution_id: str
    chat_id: str
    trigger_message_id: str
    workflow_id: str
    workflow_version: str
    requested_by: str
    references: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    job_ids: tuple[str, ...] = ()
    state: WorkflowExecutionState = WorkflowExecutionState.QUEUED
    active_phases: tuple[str, ...] = ()
    control_sequence: int = 0
    intent_epoch: int = 0
    steering_message_ids: tuple[str, ...] = ()
    updated_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "references", normalise_references(self.references))
        for name in (
            "execution_id",
            "chat_id",
            "trigger_message_id",
            "workflow_id",
            "requested_by",
        ):
            _validate_id(getattr(self, name), name)
        _validate_text(self.workflow_version, "workflow_version", max_length=64)
        object.__setattr__(
            self, "active_phases", _normalise_ids(self.active_phases, "phase")
        )
        if (
            isinstance(self.control_sequence, bool)
            or not isinstance(self.control_sequence, int)
            or self.control_sequence < 0
        ):
            raise ChatContractError("control_sequence must be a nonnegative integer")
        if self.intent_epoch < 0:
            raise ChatContractError("intent_epoch must not be negative")
        object.__setattr__(self, "job_ids", _normalise_ids(self.job_ids, "job_id"))
        object.__setattr__(
            self,
            "steering_message_ids",
            _normalise_ids(self.steering_message_ids, "message_id"),
        )
        for name in ("updated_at", "finished_at"):
            value = getattr(self, name)
            if value is not None:
                _validate_timestamp(value, name)
        ConversationRecord.__post_init__(self)

    @property
    def phase(self) -> str | None:
        """A single current phase is defined only for a sequential frontier."""
        return self.active_phases[0] if len(self.active_phases) == 1 else None


@dataclass(frozen=True, slots=True)
class ToolJob(ConversationRecord):
    """An externally submitted tool operation whose result is never discarded.

    A harness-backed operation's execution state and results are owned by its
    harness job record under the same ``job_id``.  Tracking owns attribution,
    submission certainty, and late-result observation only: ``state`` is a
    tracking lifecycle, never the execution-state authority for a harness
    execution, and ``error_message`` never asserts that a harness execution
    failed.
    """

    job_id: str
    execution_id: str
    chat_id: str
    tool_name: str
    submitted_by: str
    attribution: Mapping[str, object] = field(default_factory=dict)
    references: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    intent_epoch: int = 0
    state: ToolJobState = ToolJobState.SUBMITTED
    result: Mapping[str, object] | None = None
    error_message: str | None = None
    late_result: bool = False
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "references", normalise_references(self.references))
        for name in (
            "job_id",
            "execution_id",
            "chat_id",
            "tool_name",
            "submitted_by",
        ):
            _validate_id(getattr(self, name), name)
        object.__setattr__(
            self, "attribution", _normalise_attribution(self.attribution)
        )
        if self.intent_epoch < 0:
            raise ChatContractError("intent_epoch must not be negative")
        if self.result is not None:
            object.__setattr__(self, "result", MappingProxyType(dict(self.result)))
        if self.error_message is not None:
            _validate_text(self.error_message, "error_message", max_length=2048)
        if self.updated_at is not None:
            _validate_timestamp(self.updated_at, "updated_at")
        ConversationRecord.__post_init__(self)

    @property
    def submission_certainty(self) -> ExecutionCertainty:
        """Return tracking's certainty about whether execution started.

        A certainty retained from the observed outcome wins; an accepted but
        unstarted operation is ``not_started``, and everything else defaults to
        ``unknown_outcome`` rather than claiming an observed failure.
        """
        declared = (self.result or {}).get("executionCertainty")
        if declared in ("not_started", "observed_failure", "unknown_outcome"):
            return cast(ExecutionCertainty, declared)
        if self.state is ToolJobState.SUBMITTED:
            return "not_started"
        return "unknown_outcome"


@dataclass(frozen=True, slots=True, kw_only=True)
class SubAgentRecord(ConversationRecord):
    """An independently durable agent attached directly to one chat.

    A sub-agent owns its own conversation, specification snapshot, tool policy,
    invocation history, and result.  ``workflow_execution_id`` and
    ``phase_attempt_id`` are optional attribution links; they never make the
    workflow the owner of the sub-agent.
    """

    sub_agent_id: str
    chat_id: str
    role: str
    parent_sub_agent_id: str | None = None
    spawned_by_invocation_id: str | None = None
    workflow_execution_id: str | None = None
    phase_attempt_id: str | None = None
    phase: str | None = None
    spec_version: str = "1.0.0"
    kind: SubAgentKind = SubAgentKind.CONVERSATION
    completion_mode: CompletionMode = CompletionMode.EXPLICIT_COMPLETION
    returns_to_sub_agent_id: str | None = None
    phase_executor: bool = False
    allowed_tools: tuple[str, ...] = ()
    status: SubAgentStatus = SubAgentStatus.PENDING
    accepts_input: bool = False
    input_contract_ref: str | None = None
    completion_contract_ref: str | None = None
    result_ref: str | None = None
    result: Mapping[str, object] | None = None
    delegated_budget: int = 0
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_id(self.sub_agent_id, "sub_agent_id")
        _validate_id(self.chat_id, "chat_id")
        _validate_text(self.role, "role", max_length=256)
        for name in (
            "parent_sub_agent_id",
            "returns_to_sub_agent_id",
            "spawned_by_invocation_id",
            "workflow_execution_id",
            "phase_attempt_id",
            "phase",
            "input_contract_ref",
            "completion_contract_ref",
            "result_ref",
        ):
            value = getattr(self, name)
            if value is not None:
                _validate_id(value, name)
        _validate_text(self.spec_version, "spec_version", max_length=64)
        object.__setattr__(
            self, "completion_mode", CompletionMode(self.completion_mode)
        )
        if self.phase_executor and not (
            self.phase and self.workflow_execution_id and self.phase_attempt_id
        ):
            raise ChatContractError(
                "phase_executor requires phase execution attribution"
            )
        if not isinstance(self.kind, SubAgentKind):
            try:
                object.__setattr__(self, "kind", SubAgentKind(self.kind))
            except ValueError as error:
                raise ChatContractError(
                    "kind must be 'conversation' or 'one_shot'"
                ) from error
        if not isinstance(self.status, SubAgentStatus):
            try:
                object.__setattr__(self, "status", SubAgentStatus(self.status))
            except ValueError as error:
                raise ChatContractError("status must be a sub-agent status") from error
        if (
            self.kind is SubAgentKind.ONE_SHOT
            and self.accepts_input
            and (self.status is not SubAgentStatus.NEEDS_INPUT)
        ):
            raise ChatContractError(
                "a running one-shot sub-agent must not accept unsolicited input"
            )
        object.__setattr__(
            self, "allowed_tools", _normalise_ids(self.allowed_tools, "allowed_tool")
        )
        if self.delegated_budget < 0:
            raise ChatContractError("delegated_budget must not be negative")
        if self.result is not None:
            if not isinstance(self.result, Mapping):
                raise ChatContractError("result must be a mapping")
            object.__setattr__(self, "result", MappingProxyType(dict(self.result)))
        if self.updated_at is not None:
            _validate_timestamp(self.updated_at, "updated_at")
        ConversationRecord.__post_init__(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentInvocation(ConversationRecord):
    """One first-class durable model or tool invocation.

    Invocations are never inferred from UI events: each completion and each tool
    call is persisted with its trusted attribution, hashes, and terminal state so
    recovery can deduplicate side effects by ``idempotency_key``.

    Model invocations retain ordered ``request_messages`` references with
    message_id, direction and position, not another transcript. ``provider_request``
    is the detached actual provider audit; references never reconstruct it.
    """

    invocation_id: str
    chat_id: str
    conversation_id: str
    kind: AgentInvocationKind
    state: AgentInvocationState = AgentInvocationState.REQUESTED
    sub_agent_id: str | None = None
    parent_invocation_id: str | None = None
    workflow_execution_id: str | None = None
    phase_attempt_id: str | None = None
    role: str | None = None
    model_profile: str | None = None
    tool_name: str | None = None
    policy_hash: str | None = None
    input_hash: str | None = None
    request_messages: tuple[Mapping[str, object], ...] = ()
    provider_request: Mapping[str, object] | None = None
    request_message_id: str | None = None
    tool_request_id: str | None = None
    output_hash: str | None = None
    idempotency_key: str | None = None
    intent_epoch: int = 0
    failure: Mapping[str, object] | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.intent_epoch < 0:
            raise ChatContractError("intent_epoch must not be negative")
        for name in (
            "invocation_id",
            "chat_id",
            "conversation_id",
            "sub_agent_id",
            "parent_invocation_id",
            "workflow_execution_id",
            "phase_attempt_id",
            "request_message_id",
        ):
            value = getattr(self, name)
            if value is not None:
                _validate_id(value, name)
        for enum_type in (AgentInvocationKind, AgentInvocationState):
            value = self.kind if enum_type is AgentInvocationKind else self.state
            if not isinstance(value, enum_type):
                try:
                    coerced = enum_type(value)
                except ValueError as error:
                    raise ChatContractError(
                        f"{enum_type.__name__} value is invalid"
                    ) from error
                object.__setattr__(
                    self,
                    "kind" if enum_type is AgentInvocationKind else "state",
                    coerced,
                )
        for name in ("role", "model_profile", "tool_name", "idempotency_key"):
            value = getattr(self, name)
            if value is not None:
                _validate_text(value, name, max_length=256)
        if self.tool_request_id is not None:
            _validate_text(self.tool_request_id, "tool_request_id", max_length=4096)
        if not isinstance(self.request_messages, tuple | list) or any(
            not isinstance(message, Mapping)
            or set(message) != {"message_id", "direction", "position"}
            or not isinstance(message["message_id"], str)
            or not message["message_id"]
            or message["direction"] not in {"incoming", "outgoing", "instruction"}
            or isinstance(message["position"], bool)
            or not isinstance(message["position"], int)
            or message["position"] < 0
            for message in self.request_messages
        ):
            raise ChatContractError(
                "request_messages must contain current message references"
            )

        def freeze(value: object) -> object:
            if isinstance(value, Mapping):
                return MappingProxyType(
                    {key: freeze(item) for key, item in value.items()}
                )
            if isinstance(value, list):
                return tuple(freeze(item) for item in value)
            return value

        try:
            snapshot = json.loads(
                json.dumps(_json_value(self.request_messages), allow_nan=False)
            )
        except (TypeError, ValueError) as error:
            raise ChatContractError(
                "request_messages must contain finite JSON values"
            ) from error
        object.__setattr__(self, "request_messages", freeze(snapshot))
        if self.provider_request is not None:
            if not isinstance(self.provider_request, Mapping):
                raise ChatContractError("provider_request must be a mapping")
            try:
                audit = json.loads(
                    json.dumps(_json_value(self.provider_request), allow_nan=False)
                )
            except (TypeError, ValueError) as error:
                raise ChatContractError(
                    "provider_request must contain finite JSON values"
                ) from error
            object.__setattr__(self, "provider_request", freeze(audit))
        for name in ("policy_hash", "input_hash", "output_hash"):
            value = getattr(self, name)
            if value is not None and _HASH_PATTERN.fullmatch(value) is None:
                raise ChatContractError(f"{name} must be a lowercase SHA-256 hash")
        if self.kind is AgentInvocationKind.TOOL and not self.tool_name:
            raise ChatContractError("a tool invocation requires tool_name")
        if self.failure is not None:
            if not isinstance(self.failure, Mapping):
                raise ChatContractError("failure must be a mapping")
            object.__setattr__(self, "failure", MappingProxyType(dict(self.failure)))
        if self.updated_at is not None:
            _validate_timestamp(self.updated_at, "updated_at")
        ConversationRecord.__post_init__(self)


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
    if isinstance(record, ConversationRecord):
        for name in (
            "chat_id",
            "conversation_id",
            "root_conversation_id",
            "parent_conversation_id",
        ):
            if name in changes and changes[name] != getattr(record, name):
                raise ChatContractError("conversation ownership is immutable")
    return replace(record, **changes)


__all__ = [
    "AgentInvocation",
    "AgentInvocationKind",
    "AgentInvocationState",
    "Attachment",
    "AttachmentState",
    "ChatContractError",
    "ChatMessage",
    "ChatState",
    "ChatThread",
    "CompletionMode",
    "ConversationRecord",
    "CrossWorkspaceLinkError",
    "InvalidLifecycleTransition",
    "MessageRole",
    "MessageState",
    "normalise_reasoning",
    "RecordLinks",
    "RecordNotFound",
    "SubAgentKind",
    "SubAgentRecord",
    "SubAgentStatus",
    "WORKFLOW_NODE_ROLE",
    "RECORD_ANCHOR_ROLES",
    "ToolJob",
    "ToolJobConflictError",
    "ToolJobState",
    "TranscriptItemKind",
    "WorkflowDefinition",
    "WorkflowExecution",
    "WorkflowExecutionState",
    "WorkspaceRecord",
    "replace_record",
]
