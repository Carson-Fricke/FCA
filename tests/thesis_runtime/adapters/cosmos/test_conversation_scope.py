"""Workspace-scoped catalog queries and durable serialized root deletion."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier, Event, Lock, get_ident
from typing import Any, TypedDict, cast

import pytest
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer

from thesis_runtime.adapters.cosmos.ownership import CosmosOwnershipStore
from thesis_runtime.adapters.cosmos.records import CosmosChatPersistence
from thesis_runtime.adapters.cosmos.timeline import CosmosTimeline
from thesis_runtime.contracts.codec import record_payload
from thesis_runtime.contracts.records import (
    ChatContractError,
    ChatMessage,
    ChatState,
    ChatThread,
    MessageRole,
    SubAgentRecord,
)
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.delivery import Anchor
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.storage.ownership import own

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


@pytest.mark.parametrize(
    "owned_message", [False, True], ids=["operator-versus-root", "root-versus-phase"]
)
def test_concurrent_message_and_progress_commits_do_not_deadlock(
    owned_message: bool,
) -> None:
    class RowLockingContainer(LimitedContainer):
        def __init__(self):
            super().__init__()
            self.armed = False
            self.guard = Lock()
            self.row_locks: dict[str, Lock] = {}
            self.first: dict[int, tuple[str, Event]] = {}
            self.barrier = Barrier(2)
            self.starts = 0

        def execute_item_batch(self, *, batch_operations, partition_key):
            ids = [
                op[1][0]["id"] if op[0] in {"create", "upsert"} else op[1][0]
                for op in batch_operations
            ]
            shared = [
                item for item in ids if item.startswith(("ChatThread:", "counter:"))
            ]
            with self.guard:
                racing = self.armed and self.starts < 2 and len(shared) == 2
                if racing:
                    self.starts += 1
                    self.first[get_ident()] = (shared[0], Event())
                locks = [self.row_locks.setdefault(item, Lock()) for item in shared]
            if racing:
                self.barrier.wait(2)
            held = []
            try:
                for index, lock in enumerate(locks):
                    if not lock.acquire(timeout=1):
                        raise RuntimeError("database deadlock: reversed document locks")
                    held.append(lock)
                    if racing and index == 0:
                        mine, ready = self.first[get_ident()]
                        ready.set()
                        other, other_ready = next(
                            value
                            for key, value in self.first.items()
                            if key != get_ident()
                        )
                        if mine != other:
                            assert other_ready.wait(1)
                return super().execute_item_batch(
                    batch_operations=batch_operations, partition_key=partition_key
                )
            finally:
                for lock in reversed(held):
                    lock.release()

    database = RowLockingContainer()
    container = cast(ContainerProxy, database)
    timeline = CosmosTimeline(container)
    chat = ChatService(
        CosmosChatPersistence(container, timeline=timeline), timeline=timeline
    )
    root = chat.create_chat("workspace", title="Race", owner_actor_id="operator")
    service = Conversations(chat=chat, spawn_thread=lambda _: None)
    ownership = CosmosOwnershipStore(container)
    output_claim = ownership.acquire("workspace", "phase-turn", 60)
    message_claim = ownership.acquire("workspace", "root-turn", 60)
    assert output_claim is not None and message_claim is not None

    def output():
        with own(ownership, output_claim):
            timeline.append(
                workspace_id="workspace",
                chat_id=root.chat_id,
                kind="output",
                item_id="progress",
                payload={"state": "delta", "text": "reasoning"},
            )

    def message():
        def send():
            service.root("workspace", root.chat_id).send(
                "Steering", "operator", schedule=False
            )

        if owned_message:
            with own(ownership, message_claim):
                send()
        else:
            send()

    database.armed = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(output), pool.submit(message)]
        for future in futures:
            future.result(timeout=4)
    rows = timeline.read_after(
        workspace_id="workspace", chat_id=root.chat_id, after_seq=0, limit=10
    )
    assert len(rows) == 2
    assert {row["seq"] for row in rows} == {1, 2}
    assert {row["kind"] for row in rows} == {"message", "output"}
    assert len(service.transcript(Anchor("workspace", root.chat_id))) == 1


def test_fence_retry_retains_data_cas_when_sdk_consumes_batch_options() -> None:
    class ConsumingSdkContainer(LimitedContainer):
        before_commit = None

        def execute_item_batch(self, *, batch_operations, partition_key):
            # azure.cosmos._base._format_batch_operations pops conditional
            # options before sending the request, including failed requests.
            formatted = deepcopy(batch_operations)
            for operation in batch_operations:
                if len(operation) > 2:
                    operation[2].pop("if_match_etag", None)
            callback, self.before_commit = self.before_commit, None
            if callback is not None:
                callback()
            return super().execute_item_batch(
                batch_operations=formatted, partition_key=partition_key
            )

    database = ConsumingSdkContainer()
    container = cast(ContainerProxy, database)
    timeline = CosmosTimeline(container)
    chat = ChatService(
        CosmosChatPersistence(container, timeline=timeline), timeline=timeline
    )
    root = chat.create_chat("workspace", title="Retry", owner_actor_id="operator")
    service = Conversations(chat=chat, spawn_thread=lambda _: None)
    handle = service.root("workspace", root.chat_id)
    handle.send("Initial", "operator", schedule=False)
    database.before_commit = lambda: handle.send("Steering", "operator", schedule=False)
    timeline.append(
        workspace_id="workspace",
        chat_id=root.chat_id,
        kind="output",
        item_id="progress",
        payload={"state": "delta", "text": "reasoning"},
    )
    rows = timeline.read_after(workspace_id="workspace", chat_id=root.chat_id)
    assert [row["seq"] for row in rows] == [1, 2, 3]
    assert [row["kind"] for row in rows] == ["message", "message", "output"]
    assert [message.body for message in handle.transcript()] == ["Initial", "Steering"]


class Catalog:
    """Small workspace-partitioned catalog fake enforcing indexed scope predicates."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.queries: list[str] = []
        self.revision = 0

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
            elif verb == "upsert":
                self.upsert_item(args[0])
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
        conversation_id=chat_id,
        root_conversation_id=chat_id,
        parent_conversation_id=None,
        **COMMON,
        chat_id=chat_id,
        title="Root",
        owner_actor_id="operator",
    )


def message(
    identity: str = "message", chat_id: str = "root", child: str | None = "child"
) -> ChatMessage:
    return ChatMessage(
        conversation_id=child or chat_id,
        root_conversation_id=chat_id,
        parent_conversation_id=chat_id if child else None,
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
        SubAgentRecord(
            **COMMON,
            conversation_id="child",
            root_conversation_id="root",
            parent_conversation_id="root",
            chat_id="root",
            sub_agent_id="child",
            role="helper",
        ),
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
    assert ("workspace", "artifact:one") in catalog.items
    with pytest.raises(ChatContractError, match="retained"):
        restarted.delete(ChatThread, "workspace", "root")


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
    with pytest.raises(ChatContractError, match="requires"):
        store.purge_conversation("workspace", "root")
