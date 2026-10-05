"""Workspace-scoped catalog queries and durable serialized root deletion."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from typing import Any, TypedDict, cast

import pytest
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from thesis.chat.contracts import (
    ChatContractError,
    ChatMessage,
    ChatState,
    ChatThread,
    Investigation,
    MessageRole,
    SubAgentRecord,
)
from thesis.chat.persistence import record_payload
from thesis.cosmos.records import CosmosChatPersistence
from thesis.cosmos.timeline import CosmosTimeline

NOW = datetime(2026, 10, 3, tzinfo=UTC)


class Provenance(TypedDict):
    workspace_id: str
    created_by: str
    created_at: datetime


COMMON: Provenance = {
    "workspace_id": "workspace",
    "created_by": "operator",
    "created_at": NOW,
}


class Catalog:
    """Small workspace-partitioned catalog fake enforcing indexed scope predicates."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.queries: list[str] = []
        self.revision = 0
        self.patch_started = Event()
        self.release_patch = Event()
        self.pause_patch = False

    def _store(self, body: dict[str, Any]) -> dict[str, Any]:
        self.revision += 1
        stored = deepcopy({**body, "_etag": str(self.revision)})
        self.items[(stored["workspaceId"], stored["id"])] = stored
        return deepcopy(stored)

    def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        if (partition_key, item) not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        return deepcopy(self.items[(partition_key, item)])

    def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        if (body["workspaceId"], body["id"]) in self.items:
            raise CosmosResourceExistsError(status_code=409)
        return self._store(body)

    def upsert_item(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._store(body)

    def replace_item(
        self, *, item: str, body: dict[str, Any], etag: str, match_condition: object
    ) -> dict[str, Any]:
        previous = self.items[(body["workspaceId"], item)]
        if previous["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412)
        return self._store(body)

    def patch_item(
        self,
        *,
        item: str,
        partition_key: str,
        patch_operations: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        if self.pause_patch:
            self.patch_started.set()
            assert self.release_patch.wait(timeout=5)
        body = self.read_item(item=item, partition_key=partition_key)
        for operation in patch_operations:
            assert operation["op"] == "set"
            assert operation["path"].startswith("/payload/")
            target = body
            keys = operation["path"].strip("/").split("/")
            for key in keys[:-1]:
                target = target[key]
            target[keys[-1]] = operation["value"]
        self.upsert_item(body)

    def delete_item(self, *, item: str, partition_key: str) -> None:
        self.items.pop((partition_key, item), None)

    def execute_item_batch(
        self, *, batch_operations: list[Any], partition_key: str
    ) -> None:
        for verb, args, *_ in batch_operations:
            if verb == "delete":
                self.delete_item(item=args[0], partition_key=partition_key)
            elif verb == "create":
                self.create_item(args[0])
            else:
                self._store(args[1])

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, Any]],
        partition_key: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        self.queries.append(query)
        assert "SELECT *" not in query
        values = {parameter["name"]: parameter["value"] for parameter in parameters}
        assert partition_key == values["@ws"]
        selected = []
        for (workspace, _), item in self.items.items():
            if workspace != partition_key:
                continue
            if "@type" in values and item.get("recordType") != values["@type"]:
                continue
            if "@root" in values and item.get("rootConversationId") != values["@root"]:
                continue
            if (
                "@conversation" in values
                and item.get("conversationId") != values["@conversation"]
            ):
                continue
            if (
                "@chat" in values
                and "c.chatId = @chat" in query
                and item.get("chatId") != values["@chat"]
            ):
                continue
            if (
                "@thread" in values
                and "c.id != @thread" in query
                and item["id"] == values["@thread"]
            ):
                continue
            payload = item.get("payload", {})
            if (
                "c.payload.state != 'deleted'" in query
                and payload.get("state") == "deleted"
            ):
                continue
            selected.append(
                {"id": item["id"]}
                if "SELECT TOP @count c.id" in query
                else {"payload": deepcopy(payload)}
            )
        return selected[: values["@count"]] if "@count" in values else selected


def store_for(catalog: Catalog) -> CosmosChatPersistence:
    container = cast(ContainerProxy, catalog)
    return CosmosChatPersistence(container, timeline=CosmosTimeline(container))


def root(chat_id: str = "root") -> ChatThread:
    return ChatThread(
        **COMMON, chat_id=chat_id, title="Root", owner_actor_id="operator"
    )


def message(
    identity: str = "message", chat_id: str = "root", child: str | None = "child"
) -> ChatMessage:
    return ChatMessage(
        **COMMON,
        chat_id=chat_id,
        message_id=identity,
        actor_id="operator",
        role=MessageRole.USER,
        body="Hello",
        data={"subAgentId": child} if child else None,
    )


def test_conversation_query_is_backend_scoped_by_indexed_ownership() -> None:
    catalog = Catalog()
    store = store_for(catalog)
    for record in (
        root(),
        root("other"),
        message(),
        message("root-message", child=None),
        message("foreign", "other"),
        SubAgentRecord(**COMMON, chat_id="root", sub_agent_id="child", role="helper"),
    ):
        store.save(record)
    assert [
        record.message_id
        for record in store.list_conversation(ChatMessage, "workspace", "root")
    ] == ["message", "root-message"]
    assert [
        record.message_id
        for record in store.list_conversation(
            ChatMessage, "workspace", "root", conversation_id="child"
        )
    ] == ["message"]
    assert [
        record.message_id
        for record in store.list_conversation(
            ChatMessage, "workspace", "root", conversation_id="root"
        )
    ] == ["root-message"]
    assert (
        len(
            store.list_conversation(
                SubAgentRecord, "workspace", "root", conversation_id="child"
            )
        )
        == 1
    )
    assert all(query.startswith("SELECT c.payload") for query in catalog.queries)
    assert all("IS_DEFINED" not in query for query in catalog.queries)


def test_durable_tombstone_fences_new_adapter_and_keeps_research_and_foreign_records() -> (
    None
):
    catalog = Catalog()
    store = store_for(catalog)
    store.save(root())
    store.save(root("other"))
    for identity in ("one", "two", "three"):
        store.save(message(identity))
    store.save(message("foreign", "other"))
    investigation = Investigation(
        **COMMON,
        investigation_id="research",
        title="Research",
        objective="Investigate",
        owner_actor_id="operator",
    )
    store.save(investigation)
    catalog.items[("workspace", "artifact:one")] = {
        "id": "artifact:one",
        "workspaceId": "workspace",
        "recordType": "Artifact",
        "payload": {"chat_id": "root"},
    }
    store.mark_conversation_deleted("workspace", "root", NOW)
    restarted = store_for(catalog)
    deleted = restarted.get(ChatThread, "workspace", "root")
    assert deleted == replace(root(), state=ChatState.DELETED, updated_at=NOW)
    assert [thread.chat_id for thread in restarted.list(ChatThread, "workspace")] == [
        "other"
    ]
    assert not restarted.list_conversation(ChatThread, "workspace", "root")
    for record in (root(), message("late"), message("one")):
        with pytest.raises(ChatContractError, match="deleted"):
            restarted.save(record)
    restarted.mark_conversation_deleted("workspace", "root", NOW)
    assert not restarted.purge_conversation("workspace", "root", limit=2)
    assert restarted.purge_conversation("workspace", "root", limit=2)
    assert restarted.get(ChatThread, "workspace", "root") == deleted
    assert restarted.get(ChatMessage, "workspace", "foreign") is not None
    assert restarted.get(Investigation, "workspace", "research") == investigation
    assert ("workspace", "artifact:one") in catalog.items
    with pytest.raises(ChatContractError, match="retained"):
        restarted.delete(ChatThread, "workspace", "root")


def test_delete_and_late_write_are_serialized_across_adapter_instances() -> None:
    catalog = Catalog()
    store = store_for(catalog)
    store.save(root())
    catalog.pause_patch = True
    write_started = Event()

    def late_write() -> None:
        write_started.set()
        store_for(catalog).save(message("late"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        deleting = pool.submit(
            store.mark_conversation_deleted, "workspace", "root", NOW
        )
        assert catalog.patch_started.wait(timeout=5)
        writing = pool.submit(late_write)
        assert write_started.wait(timeout=5)
        assert not writing.done()
        catalog.release_patch.set()
        deleting.result(timeout=5)
        with pytest.raises(ChatContractError, match="deleted"):
            writing.result(timeout=5)
    assert ("workspace", "ChatMessage:late") not in catalog.items


def test_missing_root_not_recreated_and_ownership_cannot_move() -> None:
    catalog = Catalog()
    store = store_for(catalog)
    store.mark_conversation_deleted("workspace", "missing", NOW)
    assert not catalog.items
    store.save(message())
    with pytest.raises(ChatContractError, match="immutable"):
        store.save(replace(message(), parent_conversation_id="parent"))
    assert catalog.items[("workspace", "ChatMessage:message")][
        "payload"
    ] == record_payload(message())
    with pytest.raises(TypeError, match="not conversation-owned"):
        store.list_conversation(Investigation, "workspace", "root")
    with pytest.raises(ChatContractError, match="requires"):
        store.purge_conversation("workspace", "root")
