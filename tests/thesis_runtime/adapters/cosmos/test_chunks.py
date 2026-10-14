"""Cosmos item budgets, immutable payload trees, and transparent record hydration."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from threading import RLock
from typing import Any, TypedDict, cast
from unittest.mock import Mock, patch

import pytest
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from thesis_runtime.adapters.cosmos.chunks import (
    CHUNK_TYPE,
    MANIFEST_KEY,
    MAX_ITEM_BYTES,
    ChunkedPayloadStore,
    json_bytes,
)
from thesis_runtime.adapters.cosmos.client import CosmosSettings
from thesis_runtime.adapters.cosmos.events import CosmosExecutionEventStore
from thesis_runtime.adapters.cosmos.execution_store import CosmosExecutionStore
from thesis_runtime.adapters.cosmos.records import CosmosChatPersistence
from thesis_runtime.adapters.cosmos.stack import (
    open_cosmos_chat_stack,
    open_execution_store,
)
from thesis_runtime.adapters.cosmos.timeline import CosmosTimeline
from thesis_runtime.contracts.codec import Record, record_identity
from thesis_runtime.contracts.records import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
    Attachment,
    ChatMessage,
    ChatState,
    ChatThread,
    MessageRole,
    RecordLinks,
    SubAgentRecord,
    ToolJob,
    WorkflowExecution,
)
from thesis_runtime.execution.events import ExecutionEvent
from thesis_runtime.workflows.execution_store import (
    ExecutionIdentity,
    ExecutionStoreConflict,
    StoreTransition,
)

NOW = datetime(2026, 10, 3, tzinfo=UTC)
LARGE = '\u6f22\U0001f642\u00e9"\\' * 80_000


class Provenance(TypedDict):
    workspace_id: str
    created_by: str
    created_at: datetime


class Ownership(Provenance):
    chat_id: str
    root_conversation_id: str


class LimitedContainer:
    """Reject every write above Cosmos's 2 MiB limit, including escaped Unicode."""

    def __init__(self, partition_field: str | None = None) -> None:
        self.partition_field = partition_field
        self.lock = RLock()
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.writes: list[dict[str, Any]] = []
        self.revision = 0
        self.fail_chunk_after: int | None = None
        self.fail_record = False
        self.queries: list[str] = []
        self.reads: list[str] = []
        self.batches: list[list[Any]] = []
        self.feed_reads = 0
        self.feed_keys: list[str] = []

    def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        self.reads.append(item)
        try:
            return deepcopy(self.items[(partition_key, item)])
        except KeyError:
            raise CosmosResourceNotFoundError(status_code=404) from None

    def _store(self, body: dict[str, Any]) -> dict[str, Any]:
        assert len(json.dumps(body, ensure_ascii=True).encode()) < 2 * 1024 * 1024
        chunk = body.get("recordType") == CHUNK_TYPE
        if chunk and self.fail_chunk_after is not None:
            if self.fail_chunk_after == 0:
                raise RuntimeError("chunk write failed")
            self.fail_chunk_after -= 1
        if not chunk and self.fail_record:
            raise RuntimeError("record write failed")
        self.revision += 1
        stored = deepcopy({**body, "_etag": str(self.revision)})
        partition = (
            body[self.partition_field]
            if self.partition_field
            else body.get("workspaceId", body.get("chatId", body.get("executionId")))
        )
        self.items[(partition, body["id"])] = stored
        self.writes.append(deepcopy(body))
        return deepcopy(stored)

    def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        partition = (
            body[self.partition_field]
            if self.partition_field
            else body.get("workspaceId", body.get("chatId", body.get("executionId")))
        )
        if (partition, body["id"]) in self.items:
            raise CosmosResourceExistsError(status_code=409)
        return self._store(body)

    def upsert_item(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._store(body)

    def replace_item(
        self, *, item: str, body: dict[str, Any], etag: str, **kwargs: Any
    ) -> dict[str, Any]:
        partition = (
            body[self.partition_field] if self.partition_field else body["workspaceId"]
        )
        if self.items[(partition, item)]["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412)
        return self._store(body)

    def patch_item(
        self,
        *,
        item: str,
        partition_key: str,
        patch_operations: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        body = self.read_item(item=item, partition_key=partition_key)
        if "etag" in kwargs and body["_etag"] != kwargs["etag"]:
            raise CosmosAccessConditionFailedError(status_code=412)
        for operation in patch_operations:
            target = body
            keys = operation["path"].strip("/").split("/")
            for key in keys[:-1]:
                target = target[key]
            target[keys[-1]] = operation["value"]
        return self._store(body)

    def delete_item(self, *, item: str, partition_key: str) -> None:
        self.items.pop((partition_key, item), None)

    def execute_item_batch(
        self, *, batch_operations: list[Any], partition_key: str
    ) -> None:
        with self.lock:
            self.batches.append(deepcopy(batch_operations))
            # A fake external database must honor transaction atomicity and CAS.
            for index, (verb, args, *options) in enumerate(batch_operations):
                item_id = args[0]["id"] if verb in {"create", "upsert"} else args[0]
                existing = self.items.get((partition_key, item_id))
                status = 409 if verb == "create" and existing is not None else None
                etag = options[0].get("if_match_etag") if options else None
                if etag is not None and (existing is None or existing["_etag"] != etag):
                    status = 412
                if status:
                    from azure.cosmos.exceptions import CosmosBatchOperationError

                    raise CosmosBatchOperationError(
                        error_index=index, status_code=status, headers={}
                    )
            keys = {
                (
                    partition_key,
                    args[0]["id"] if verb in {"create", "upsert"} else args[0],
                )
                for verb, args, *_ in batch_operations
            }
            previous = {key: self.items.get(key) for key in keys}
            try:
                for verb, args, *_ in batch_operations:
                    if verb == "delete":
                        self.delete_item(item=args[0], partition_key=partition_key)
                    elif verb == "create":
                        self.create_item(args[0])
                    elif verb == "upsert":
                        self.upsert_item(args[0])
                    else:
                        self._store(args[1])
            except Exception:
                for key, value in previous.items():
                    if value is None:
                        self.items.pop(key, None)
                    else:
                        self.items[key] = value
                raise

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, Any]],
        partition_key: str | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        self.queries.append(query)
        values = {parameter["name"]: parameter["value"] for parameter in parameters}
        selected = []
        for (partition, _), item in self.items.items():
            if partition_key is not None and partition != partition_key:
                continue
            if (
                "c.recordType = 'RuntimeWork'" in query
                and item.get("recordType") != "RuntimeWork"
            ):
                continue
            if "c.state IN" in query and item.get("state") not in {
                "pending",
                "running",
            }:
                continue
            if "@type" in values and item.get("recordType") != values["@type"]:
                continue
            if "@id" in values and item["id"] != values["@id"]:
                continue
            if (
                "@chat" in values
                and "c.chatId" in query
                and item.get("chatId") != values["@chat"]
            ):
                continue
            if (
                "@thread" in values
                and "c.id != @thread" in query
                and item["id"] == values["@thread"]
            ):
                continue
            if "@exec" in values and item.get("executionId") != values["@exec"]:
                continue
            if "@kind" in values and item.get("kind") != values["@kind"]:
                continue
            if (
                "@after" in values
                and "c.seq > @after" in query
                and ("seq" not in item or item["seq"] <= values["@after"])
            ):
                continue
            if (
                "@before" in values
                and "c.seq < @before" in query
                and ("seq" not in item or item["seq"] >= values["@before"])
            ):
                continue
            if "@counter" in values and item.get("kind") == values["@counter"]:
                continue
            if (
                "@thread" in values
                and "c.threadId = @thread" in query
                and (item.get("threadId") != values["@thread"])
            ):
                continue
            if (
                "@position" in values
                and item.get("inputPosition", 0) <= values["@position"]
            ):
                continue
            if "@resource" in values and item.get("resource") != values["@resource"]:
                continue
            payload = item.get("payload", {})
            if "@root" in values and item.get("rootConversationId") != values["@root"]:
                continue
            if (
                "@conversation" in values
                and item.get("conversationId") != values["@conversation"]
            ):
                continue
            if (
                "c.payload.state != 'deleted'" in query
                and payload.get("state") == "deleted"
            ):
                continue
            if "c.payload.state = 'deleted'" in query and (
                item.get("recordType") != "ChatThread"
                or payload.get("state") != "deleted"
            ):
                continue
            selected.append(
                {"id": item["id"]}
                if "SELECT TOP @count c.id" in query
                else deepcopy(item)
            )
        if "ORDER BY c.createdAt" in query:
            selected.sort(key=lambda item: item["createdAt"])
        if "ORDER BY c.seq" in query:
            selected.sort(key=lambda item: item.get("seq", 0), reverse="DESC" in query)
        if "SELECT VALUE MAX(c.seq)" in query:
            return [max((item["seq"] for item in selected), default=None)]
        if "@limit" in values:
            selected = selected[: values["@limit"]]
        return selected[: values["@count"]] if "@count" in values else selected

    def query_items_change_feed(self, **kwargs: Any):
        """Latest-version external feed with bounded pages and opaque progress."""
        self.feed_reads += 1
        cursor = int(kwargs.get("continuation", str(self.revision)))
        with self.lock:
            rows = sorted(
                (
                    deepcopy(row)
                    for row in self.items.values()
                    if int(row["_etag"]) > cursor
                ),
                key=lambda row: int(row["_etag"]),
            )[: kwargs["max_item_count"]]
        next_cursor = max((int(row["_etag"]) for row in rows), default=cursor)

        self.feed_keys.extend(
            row["key"] for row in rows if row.get("recordType") == "RuntimeWork"
        )
        kwargs["response_hook"]({"etag": str(next_cursor)}, {})

        class Page:
            continuation_token = str(next_cursor) if rows else None

            def by_page(self):
                return iter(self)

            def __iter__(self):
                return self

            def __next__(self):
                if not hasattr(self, "done"):
                    self.done = True
                    return iter(rows)
                raise StopIteration

        return Page()


def store_for(
    container: LimitedContainer, timeline: CosmosTimeline | None = None
) -> CosmosChatPersistence:
    resolved = (
        timeline
        if timeline is not None
        else CosmosTimeline(cast(ContainerProxy, container))
    )
    return CosmosChatPersistence(cast(ContainerProxy, container), timeline=resolved)


def oversized_records() -> list[Record]:
    common: Provenance = {
        "workspace_id": "workspace",
        "created_by": "operator",
        "created_at": NOW,
    }
    ownership: Ownership = {**common, "chat_id": "chat", "root_conversation_id": "chat"}
    identities = tuple(f"reference-{index:06}-" + "x" * 48 for index in range(34_000))
    links = RecordLinks(references={"fca.strategy": identities})
    assert len(json_bytes(links.as_dict())) > 2 * 1024 * 1024
    assert len(json.dumps({"raw": LARGE}, ensure_ascii=True).encode()) > 2 * 1024 * 1024
    return [
        ChatThread(
            conversation_id="chat",
            parent_conversation_id=None,
            **ownership,
            title="Root",
            owner_actor_id="operator",
            links=links,
        ),
        ChatMessage(
            conversation_id="child",
            parent_conversation_id="chat",
            **ownership,
            message_id="message",
            actor_id="operator",
            role=MessageRole.TOOL,
            body="Result",
            tool_result={"raw": LARGE},
            data={"subAgentId": "child"},
        ),
        Attachment(
            conversation_id="chat",
            parent_conversation_id=None,
            **ownership,
            attachment_id="attachment",
            message_id="message",
            filename="file",
            media_type="text/plain",
            size_bytes=0,
        ),
        WorkflowExecution(
            conversation_id="chat",
            parent_conversation_id=None,
            **ownership,
            execution_id="execution",
            trigger_message_id="message",
            workflow_id="workflow",
            workflow_version="1.0.0",
            requested_by="operator",
            references={"fca.strategy": identities},
        ),
        ToolJob(
            conversation_id="chat",
            parent_conversation_id=None,
            **ownership,
            job_id="job",
            execution_id="execution",
            tool_name="tool",
            submitted_by="operator",
            result={"raw": LARGE},
        ),
        SubAgentRecord(
            conversation_id="child",
            parent_conversation_id="chat",
            **ownership,
            sub_agent_id="child",
            role="helper",
            result={"raw": LARGE},
        ),
        AgentInvocation(
            parent_conversation_id=None,
            **ownership,
            conversation_id="chat",
            invocation_id="invocation",
            kind=AgentInvocationKind.MODEL,
            state=AgentInvocationState.STARTED,
            provider_request={
                "messages": [{"role": "user", "content": LARGE}],
                "tools": [{"schema": LARGE}],
            },
        ),
    ]


def test_every_record_type_round_trips_lists_and_conversation_queries() -> None:
    container = LimitedContainer()
    store = store_for(container)
    for record in oversized_records():
        store.save(record)
        kind = type(record)
        identity = record_identity(record)
        assert store.get(kind, "workspace", identity) == record
        assert store.list(kind, "workspace") == (record,)
        assert store.list_conversation(
            kind, "workspace", "chat", conversation_id=record.conversation_id
        ) == (record,)
    assert store.scope_for_execution("execution") == ("workspace", "chat")
    assert store.get(ChatMessage, "other", "message") is None
    assert all(
        len(json.dumps(item, ensure_ascii=True).encode()) < MAX_ITEM_BYTES
        for item in container.writes
    )
    assert all(
        MANIFEST_KEY in item["payload"]
        for item in container.writes
        if "payload" in item
        and item.get("recordType") not in {CHUNK_TYPE, "Attachment", "AgentInvocation"}
    )


def test_reference_tree_has_bounded_root_at_multiple_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("thesis_runtime.adapters.cosmos.chunks.CHUNK_BYTES", 1024)
    monkeypatch.setattr("thesis_runtime.adapters.cosmos.chunks.REFERENCE_FANOUT", 4)
    container = LimitedContainer()
    chunks = ChunkedPayloadStore(cast(ContainerProxy, container))
    payload = {"actual_request": LARGE[:200_000]}
    manifest = chunks.dump(payload, "workspace")
    assert len(json_bytes(manifest)) < 256
    assert chunks.load(manifest, "workspace") == payload
    pages = [item for item in container.items.values() if "children" in item]
    assert len(pages) > 4
    assert all(len(page["children"]) <= 4 for page in pages)
    count = len(container.writes)
    assert chunks.dump(payload, "workspace") == manifest
    assert len(container.writes) == count


@pytest.mark.parametrize("failure", ["chunk", "record"])
def test_failed_write_never_publishes_incomplete_payload_and_retry_reuses_chunks(
    failure: str,
) -> None:
    container = LimitedContainer()
    store = store_for(container)
    record = ChatMessage(
        workspace_id="workspace",
        created_by="operator",
        created_at=NOW,
        chat_id="chat",
        message_id="message",
        actor_id="operator",
        role=MessageRole.TOOL,
        body="Result",
        tool_result={"raw": LARGE},
        conversation_id="chat",
        root_conversation_id="chat",
        parent_conversation_id=None,
    )
    if failure == "chunk":
        container.fail_chunk_after = 1
    else:
        container.fail_record = True
    with pytest.raises(RuntimeError, match="write failed"):
        store.save(record)
    assert store.get(ChatMessage, "workspace", "message") is None
    retained = deepcopy(container.items)
    container.fail_chunk_after = None
    container.fail_record = False
    store.save(record)
    assert all(container.items[key] == value for key, value in retained.items())
    assert store.get(ChatMessage, "workspace", "message") == record
    record_position = next(
        index
        for index, item in enumerate(container.writes)
        if item.get("recordType") == "ChatMessage"
    )
    assert all(
        index < record_position
        for index, item in enumerate(container.writes)
        if item.get("recordType") == CHUNK_TYPE
    )


@pytest.mark.parametrize("path", ["get", "list", "conversation"])
def test_missing_chunks_fail_explicitly_on_all_hydration_paths(path: str) -> None:
    container = LimitedContainer()
    record = oversized_records()[-1]
    store = store_for(container)
    store.save(record)
    manifest = container.items[("workspace", "AgentInvocation:invocation")]["payload"][
        "provider_request"
    ][MANIFEST_KEY]
    del container.items[("workspace", manifest["root"])]
    with pytest.raises(ValueError, match="missing Cosmos payload chunk"):
        if path == "get":
            store.get(AgentInvocation, "workspace", "invocation")
        elif path == "list":
            store.list(AgentInvocation, "workspace")
        else:
            store.list_conversation(AgentInvocation, "workspace", "chat")


def test_corruption_and_immutable_conflicts_fail_explicitly() -> None:
    container = LimitedContainer()
    chunks = ChunkedPayloadStore(cast(ContainerProxy, container))
    payload = {"raw": LARGE}
    manifest = chunks.dump(payload, "workspace")
    leaf = next(item for item in container.items.values() if "data" in item)
    leaf["data"] = "corrupted"
    with pytest.raises(ValueError, match="corrupt"):
        chunks.load(manifest, "workspace")
    with pytest.raises(ValueError, match="immutable"):
        chunks.dump(payload, "workspace")


def test_inline_stored_payloads_and_updates_between_storage_forms() -> None:
    container = LimitedContainer()
    store = store_for(container)
    record = ChatMessage(
        workspace_id="workspace",
        created_by="operator",
        created_at=NOW,
        chat_id="chat",
        message_id="message",
        actor_id="operator",
        role=MessageRole.USER,
        body="inline",
        conversation_id="chat",
        root_conversation_id="chat",
        parent_conversation_id=None,
    )
    store.save(record)
    assert store.get(ChatMessage, "workspace", "message") == record
    assert (
        MANIFEST_KEY
        not in container.items[("workspace", "ChatMessage:message")]["payload"]
    )
    large = replace(record, tool_result={"raw": LARGE})
    store.save(large)
    retained = {
        key: deepcopy(value)
        for key, value in container.items.items()
        if value.get("recordType") == CHUNK_TYPE
    }
    assert store.get(ChatMessage, "workspace", "message") == large
    store.save(record)
    assert store.list(ChatMessage, "workspace") == (record,)
    assert all(container.items[key] == value for key, value in retained.items())
    store.delete(ChatMessage, "workspace", "message")
    assert store.get(ChatMessage, "workspace", "message") is None


def test_chunked_tombstone_patch_lists_and_purge() -> None:
    container = LimitedContainer()
    store = store_for(container)
    root = cast(ChatThread, oversized_records()[0])
    message = cast(ChatMessage, oversized_records()[1])
    store.save(root)
    store.save(message)
    store.mark_conversation_deleted("workspace", "chat", NOW)
    deleted = replace(root, state=ChatState.DELETED, updated_at=NOW)
    assert store.get(ChatThread, "workspace", "chat") == deleted
    assert store.list_deleted_conversations("workspace") == (deleted,)
    assert store.list(ChatThread, "workspace") == ()
    assert store.purge_conversation("workspace", "chat")
    assert store.get(ChatMessage, "workspace", "message") is None
    assert store.get(ChatThread, "workspace", "chat") == deleted


def test_timeline_item_update_preserves_sequence_and_snapshot() -> None:
    container = LimitedContainer("workspaceId")
    store = store_for(container)
    first = cast(ChatMessage, oversized_records()[1])
    store.save(first)
    envelope = container.items[("workspace", "ChatMessage:message")]
    assert envelope["kind"] == "message"
    assert envelope["threadId"] == "child"
    assert envelope["seq"] == 1
    updated = replace(first, tool_result={"raw": LARGE + "updated"}, intent_epoch=1)
    store.save(updated)
    envelope = container.items[("workspace", "ChatMessage:message")]
    assert envelope["seq"] == 1
    assert store.get(ChatMessage, "workspace", "message") == updated
    assert all(
        len(json.dumps(item, ensure_ascii=True).encode()) < MAX_ITEM_BYTES
        for item in container.writes
    )


def test_failed_update_keeps_previous_authoritative_record() -> None:
    container = LimitedContainer()
    store = store_for(container)
    first = cast(AgentInvocation, oversized_records()[-1])
    store.save(first)
    container.fail_record = True
    with pytest.raises(RuntimeError, match="record write failed"):
        store.save(replace(first, state=AgentInvocationState.SUCCEEDED))
    assert store.get(AgentInvocation, "workspace", "invocation") == first


def test_constructor_has_no_database_or_container_creation_effects() -> None:
    container = Mock(spec=ContainerProxy)
    timeline = CosmosTimeline(cast(ContainerProxy, container))
    CosmosChatPersistence(cast(ContainerProxy, container), timeline=timeline)
    ChunkedPayloadStore(cast(ContainerProxy, container))
    assert not container.mock_calls


def test_manifest_digest_is_checked_without_provider_conversion() -> None:
    container = LimitedContainer()
    chunks = ChunkedPayloadStore(cast(ContainerProxy, container))
    request = {
        "messages": [{"content": LARGE}],
        "unknown_provider_option": {"raw": [1, True, None]},
    }
    manifest = chunks.dump(request, "workspace")
    assert chunks.load(manifest, "workspace") == request
    manifest[MANIFEST_KEY]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="digest mismatch"):
        chunks.load(manifest, "workspace")


@pytest.mark.parametrize("path", ["after", "before", "replay"])
def test_timeline_and_event_reads_hydrate_large_payloads_and_reject_missing_chunks(
    path: str,
) -> None:
    container = LimitedContainer("workspaceId")
    timeline = CosmosTimeline(
        cast(ContainerProxy, container),
        chunks=ChunkedPayloadStore(cast(ContainerProxy, container)),
    )
    events = CosmosExecutionEventStore(
        timeline, scope_for_execution=lambda _: ("workspace", "chat")
    )
    event = ExecutionEvent(
        execution_id="execution",
        workspace_id="workspace",
        event_type="workflow.started",
        correlation_id="correlation",
        actor_id="operator",
        actor_scope="user",
        occurred_at=NOW,
        payload={"raw": LARGE},
    )
    stored = events.append(event)
    assert stored.payload == event.payload and stored.cursor == 1
    assert events.append(replace(event, payload={"different": True})) == stored
    assert events.latest_cursor("execution") == 1

    def read() -> Any:
        if path == "after":
            return timeline.read_after(
                workspace_id="workspace", chat_id="chat", limit=1
            )
        if path == "before":
            return timeline.read_before(
                workspace_id="workspace", chat_id="chat", before_seq=2, limit=1
            )
        return events.replay("execution", limit=1)

    hydrated = read()
    if path == "replay":
        assert hydrated.events == (stored,)
    else:
        assert hydrated[0]["payload"] == stored.as_dict()
    manifest = container.items[("workspace", f"event:{event.event_id}")]["payload"][
        MANIFEST_KEY
    ]
    del container.items[("workspace", manifest["root"])]
    with pytest.raises(ValueError, match="missing Cosmos payload chunk"):
        read()


def test_execution_store_large_cumulative_payloads_preserve_revision_cas_and_failures() -> (
    None
):
    container = LimitedContainer("workspaceId")
    store = CosmosExecutionStore(cast(ContainerProxy, container))
    identity = ExecutionIdentity(
        "execution", "workspace", "chat", "workflow", "1.0.0", "operator"
    )
    document = store.create(identity, launch_request={"raw": LARGE})
    assert store.get("execution") == document
    transition = StoreTransition(
        1,
        "first",
        "second",
        NOW,
        0,
        "accepted",
        {"raw": LARGE},
        attempt_id="fixture-attempt",
    )
    document = store.append_transition("execution", transition, expected_revision=1)
    document = store.put_phase_result(
        "execution", "first", {"raw": LARGE}, expected_revision=2
    )
    assert store.get("execution") == document
    with pytest.raises(ExecutionStoreConflict):
        store.put_phase_result("execution", "second", {}, expected_revision=1)
    container.fail_record = True
    with pytest.raises(RuntimeError, match="record write failed"):
        store.put_phase_result(
            "execution", "second", {"raw": LARGE + "new"}, expected_revision=3
        )
    assert store.get("execution") == document
    manifest = container.items[("workspace", "execution-store:execution")]["payload"][
        MANIFEST_KEY
    ]
    del container.items[("workspace", manifest["root"])]
    with pytest.raises(ValueError, match="missing Cosmos payload chunk"):
        store.get("execution")
    assert all(
        len(json.dumps(item, ensure_ascii=True).encode()) < MAX_ITEM_BYTES
        for item in container.writes
    )


def test_invocation_terminal_update_reuses_actual_provider_audit_chunks() -> None:
    container = LimitedContainer()
    store = store_for(container)
    invocation = cast(AgentInvocation, oversized_records()[-1])
    store.save(invocation)
    audit = deepcopy(
        container.items[("workspace", "AgentInvocation:invocation")]["payload"][
            "provider_request"
        ]
    )
    chunks = len(
        [item for item in container.writes if item.get("recordType") == CHUNK_TYPE]
    )
    terminal = replace(invocation, state=AgentInvocationState.SUCCEEDED, updated_at=NOW)
    store.save(terminal)
    assert (
        container.items[("workspace", "AgentInvocation:invocation")]["payload"][
            "provider_request"
        ]
        == audit
    )
    assert (
        len([item for item in container.writes if item.get("recordType") == CHUNK_TYPE])
        == chunks
    )
    assert store.get(AgentInvocation, "workspace", "invocation") == terminal


def test_stack_retains_bootstrap_and_injects_catalog_chunks() -> None:
    client = Mock()
    database = Mock()
    catalog = LimitedContainer("workspaceId")
    execution = LimitedContainer("workspaceId")
    database.get_container_client.side_effect = lambda name: {
        "catalog": catalog,
        "executions": execution,
    }[name]
    settings = CosmosSettings(database="existing")
    with (
        patch(
            "thesis_runtime.adapters.cosmos.stack.open_cosmos_client",
            return_value=client,
        ) as open_client,
        patch(
            "thesis_runtime.adapters.cosmos.stack.ensure_database",
            return_value=database,
        ) as bootstrap,
    ):
        _, timeline = open_cosmos_chat_stack(settings)
        store = open_execution_store(settings)
    assert open_client.call_count == 2
    open_client.assert_called_with(settings)
    assert bootstrap.call_count == 2
    bootstrap.assert_called_with(client, settings)
    payload = {"raw": LARGE}
    timeline.append(
        chat_id="chat",
        workspace_id="workspace",
        kind="message",
        payload=payload,
        item_id="large",
    )
    hydrated = timeline.read_after(workspace_id="workspace", chat_id="chat", limit=1)
    assert hydrated[0]["payload"] == payload
    assert isinstance(store, CosmosExecutionStore)


def test_event_chunk_failure_never_publishes_or_allocates_sequence() -> None:
    container = LimitedContainer("workspaceId")
    timeline = CosmosTimeline(
        cast(ContainerProxy, container),
        chunks=ChunkedPayloadStore(cast(ContainerProxy, container)),
    )
    container.fail_chunk_after = 1
    with pytest.raises(RuntimeError, match="chunk write failed"):
        timeline.append(
            chat_id="chat",
            workspace_id="workspace",
            kind="event",
            payload={"raw": LARGE},
            item_id="event:large",
        )
    assert ("workspace", "counter:chat") not in container.items
    assert ("workspace", "event:large") not in container.items
    assert timeline.latest_sequence(workspace_id="workspace", chat_id="chat") == 0


def test_large_generic_index_fields_do_not_expand_manifest() -> None:
    container = LimitedContainer("workspaceId")
    chunks = ChunkedPayloadStore(cast(ContainerProxy, container))
    payload = {"state": LARGE, "data": {"subAgentId": LARGE}, "raw": LARGE}
    manifest = chunks.dump(payload, "workspace")
    assert len(json_bytes(manifest)) < 256
    assert chunks.load(manifest, "workspace") == payload


def test_duplicate_timeline_append_does_not_require_new_payload_chunks() -> None:
    container = LimitedContainer("workspaceId")
    timeline = CosmosTimeline(
        cast(ContainerProxy, container),
        chunks=ChunkedPayloadStore(cast(ContainerProxy, container)),
    )
    first = timeline.append(
        chat_id="chat",
        workspace_id="workspace",
        kind="message",
        payload={"first": True},
        item_id="message",
    )
    container.fail_chunk_after = 0
    duplicate = timeline.append(
        chat_id="chat",
        workspace_id="workspace",
        kind="message",
        payload={"raw": LARGE},
        item_id="message",
    )
    assert duplicate["payload"] == first["payload"]
    assert duplicate["seq"] == first["seq"]
