"""Storage-independent canonical decoding for runtime-owned record types."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import fields
from typing import TypeVar

from pydantic import TypeAdapter, ValidationError

from thesis_runtime.contracts.records import (
    AgentInvocation,
    Attachment,
    ChatContractError,
    ChatMessage,
    ChatThread,
    ConversationRecord,
    SubAgentRecord,
    ToolJob,
    WorkflowExecution,
)

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


_ID_FIELDS: Mapping[RecordType, str] = {
    ChatThread: "chat_id",
    ChatMessage: "message_id",
    Attachment: "attachment_id",
    WorkflowExecution: "execution_id",
    ToolJob: "job_id",
    SubAgentRecord: "sub_agent_id",
    AgentInvocation: "invocation_id",
}


_RECORD_TYPES: tuple[RecordType, ...] = (
    ChatThread,
    ChatMessage,
    Attachment,
    WorkflowExecution,
    ToolJob,
    SubAgentRecord,
    AgentInvocation,
)


_RECORD_ADAPTERS: dict[RecordType, TypeAdapter[Record]] = {
    record_type: TypeAdapter(record_type) for record_type in _RECORD_TYPES
}


_SKIPPED_FIELDS = frozenset({"schema_version", "content_hash"})


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


def _from_payload(record_type: RecordType, data: Mapping[str, object]) -> Record:
    """Decode only a complete current record; never infer persisted fields."""
    _ensure_supported(record_type)
    required = {item.name for item in fields(record_type) if item.init}
    missing = required.difference(data)
    if missing:
        raise ChatContractError(
            f"outdated {record_type.__name__}: missing fields {', '.join(sorted(missing))}"
        )
    try:
        return _RECORD_ADAPTERS[record_type].validate_python(dict(data))
    except ValidationError as error:
        raise ChatContractError(f"invalid {record_type.__name__}: {error}") from error


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
