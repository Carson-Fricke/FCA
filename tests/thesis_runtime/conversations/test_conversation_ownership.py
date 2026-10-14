"""Durable explicit ownership, validation, and local deletion fencing."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import pytest

from thesis_runtime.adapters.filesystem.records import FileChatPersistence
from thesis_runtime.contracts.codec import record_from_payload, record_payload
from thesis_runtime.contracts.records import (
    AgentInvocation,
    AgentInvocationKind,
    Attachment,
    ChatContractError,
    ChatMessage,
    ChatState,
    ChatThread,
    MessageRole,
    SubAgentRecord,
    ToolJob,
    WorkflowExecution,
    replace_record,
)

NOW = datetime(2026, 10, 3, tzinfo=UTC)


class Provenance(TypedDict):
    workspace_id: str
    root_conversation_id: str
    created_by: str
    created_at: datetime


COMMON: Provenance = {
    "workspace_id": "workspace",
    "root_conversation_id": "root",
    "created_by": "operator",
    "created_at": NOW,
}
OwnedRecord = (
    ChatThread
    | ChatMessage
    | Attachment
    | WorkflowExecution
    | ToolJob
    | SubAgentRecord
    | AgentInvocation
)


def owned_records() -> tuple[OwnedRecord, ...]:
    return (
        ChatThread(
            conversation_id="root",
            parent_conversation_id=None,
            **COMMON,
            chat_id="root",
            title="Root",
            owner_actor_id="operator",
        ),
        ChatMessage(
            conversation_id="child",
            parent_conversation_id="root",
            **COMMON,
            chat_id="root",
            message_id="message",
            actor_id="operator",
            role=MessageRole.USER,
            body="Hello",
            data={"subAgentId": "child"},
        ),
        Attachment(
            conversation_id="root",
            parent_conversation_id=None,
            **COMMON,
            chat_id="root",
            attachment_id="upload",
            message_id="message",
            filename="test.txt",
            media_type="text/plain",
            size_bytes=1,
        ),
        WorkflowExecution(
            conversation_id="root",
            parent_conversation_id=None,
            **COMMON,
            chat_id="root",
            execution_id="execution",
            trigger_message_id="message",
            workflow_id="workflow",
            workflow_version="1.0.0",
            requested_by="operator",
        ),
        ToolJob(
            conversation_id="root",
            parent_conversation_id=None,
            **COMMON,
            chat_id="root",
            job_id="job",
            execution_id="execution",
            tool_name="tool",
            submitted_by="operator",
        ),
        SubAgentRecord(
            conversation_id="child",
            parent_conversation_id="parent",
            **COMMON,
            chat_id="root",
            sub_agent_id="child",
            role="helper",
            parent_sub_agent_id="parent",
        ),
        AgentInvocation(
            parent_conversation_id="root",
            **COMMON,
            chat_id="root",
            invocation_id="invocation",
            conversation_id="child",
            kind=AgentInvocationKind.MODEL,
        ),
    )


@pytest.mark.parametrize("record", owned_records())
def test_ownership_roundtrip_and_missing_ownership_rejection(
    record: OwnedRecord,
) -> None:
    payload = record_payload(record)
    assert record_from_payload(type(record), payload) == record
    legacy = {
        key: value
        for key, value in payload.items()
        if key not in {"root_conversation_id", "parent_conversation_id"}
        and (key != "conversation_id" or isinstance(record, AgentInvocation))
    }
    with pytest.raises(ChatContractError, match="outdated"):
        record_from_payload(type(record), legacy)


def test_ownership_is_validated_frozen_and_hashed() -> None:
    message = owned_records()[1]
    with pytest.raises(FrozenInstanceError):
        message.conversation_id = "other"  # ty: ignore[invalid-assignment]
    for changes in (
        {"root_conversation_id": "other"},
        {"conversation_id": "other"},
        {"parent_conversation_id": "child"},
        {"conversation_id": "../bad"},
    ):
        with pytest.raises(ChatContractError):
            replace(message, **changes)
    with pytest.raises(ChatContractError, match="immutable"):
        replace_record(message, parent_conversation_id="other")
    assert (
        replace(message, parent_conversation_id="parent").content_hash
        != message.content_hash
    )


def test_file_scope_tombstone_restart_and_bounded_purge(tmp_path: Path) -> None:
    store = FileChatPersistence(tmp_path)
    for record in owned_records():
        store.save(record)
    assert (
        len(
            store.list_conversation(
                ChatMessage, "workspace", "root", conversation_id="child"
            )
        )
        == 1
    )
    assert not store.list_conversation(
        ChatMessage, "workspace", "root", conversation_id="root"
    )
    with pytest.raises(ChatContractError, match="immutable"):
        store.save(replace(owned_records()[1], parent_conversation_id="parent"))
    store.mark_conversation_deleted("workspace", "root", NOW)
    restarted = FileChatPersistence(tmp_path)
    deleted = restarted.get(ChatThread, "workspace", "root")
    assert deleted is not None and deleted.state is ChatState.DELETED
    assert not restarted.list(ChatThread, "workspace")
    for record in owned_records():
        with pytest.raises(ChatContractError, match="deleted"):
            restarted.save(record)
    restarted.mark_conversation_deleted("workspace", "root", NOW)
    assert not restarted.purge_conversation("workspace", "root", limit=2)
    assert restarted.purge_conversation("workspace", "root", limit=100)
    assert restarted.get(ChatThread, "workspace", "root") is not None
    with pytest.raises(ChatContractError, match="retained"):
        restarted.delete(ChatThread, "workspace", "root")


def test_missing_file_root_is_not_recreated(tmp_path: Path) -> None:
    store = FileChatPersistence(tmp_path)
    store.mark_conversation_deleted("workspace", "missing", NOW)
    assert store.get(ChatThread, "workspace", "missing") is None


def test_files_without_ownership_are_outdated(tmp_path: Path) -> None:
    record = owned_records()[1]
    store = FileChatPersistence(tmp_path)
    store.save(record)
    path = next((tmp_path / "ChatMessage").glob("*.json"))
    document = json.loads(path.read_text(encoding="utf-8"))
    for name in ("conversation_id", "root_conversation_id", "parent_conversation_id"):
        document["record"].pop(name)
    path.write_text(json.dumps(document), encoding="utf-8")
    restarted = FileChatPersistence(tmp_path)
    with pytest.raises(ChatContractError, match="outdated"):
        restarted.list_conversation(
            ChatMessage, "workspace", "root", conversation_id="child"
        )
