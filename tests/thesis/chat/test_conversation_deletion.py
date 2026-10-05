"""Logical deletion fences a nested tree while bounded cleanup preserves artifacts."""

from datetime import UTC, datetime
from threading import Event

import pytest

from thesis.chat.contracts import (
    ChatContractError,
    ChatMessage,
    ChatState,
    ChatThread,
    Investigation,
    RecordNotFound,
    SubAgentRecord,
)
from thesis.chat.service import ChatService, InMemoryChatPersistence


def test_logical_delete_does_not_wait_for_purge_and_fences_descendants() -> None:
    entered, release = Event(), Event()

    class Persistence(InMemoryChatPersistence):
        def purge_conversation(
            self, workspace_id: str, chat_id: str, limit: int = 100
        ) -> bool:
            entered.set()
            assert release.wait(5)
            return super().purge_conversation(workspace_id, chat_id, limit)

    store = Persistence()
    service = ChatService(store)
    root = service.create_chat("workspace-a", title="Delete", owner_actor_id="operator")
    other = service.create_chat("workspace-a", title="Keep", owner_actor_id="operator")
    parent = None
    nodes = []
    for index in range(3):
        node = store.save(
            SubAgentRecord(
                workspace_id="workspace-a",
                created_at=datetime.now(UTC),
                created_by="operator",
                sub_agent_id=f"child-{index}",
                chat_id=root.chat_id,
                parent_sub_agent_id=parent,
                role="helper",
            )
        )
        nodes.append(node)
        parent = node.sub_agent_id
        message = service.create_message(
            "workspace-a",
            root.chat_id,
            actor_id="operator",
            body="Nested",
            data={"subAgentId": node.sub_agent_id},
        )
        assert message.parent_conversation_id == node.parent_conversation_id
    artifact = store.save(
        Investigation(
            workspace_id="workspace-a",
            created_at=datetime.now(UTC),
            created_by="operator",
            investigation_id="research",
            title="Retained research",
            objective="Keep evidence",
            owner_actor_id="operator",
        )
    )
    try:
        service.delete_chat("workspace-a", root.chat_id)
        assert entered.wait(2)
        assert [chat.chat_id for chat in service.list_chats("workspace-a")] == [
            other.chat_id
        ]
        with pytest.raises(RecordNotFound):
            service.list_messages("workspace-a", root.chat_id)
        for node in nodes:
            with pytest.raises(ChatContractError, match="deleted"):
                store.save(node)
        tombstone = store.get(ChatThread, "workspace-a", root.chat_id)
        assert tombstone is not None and tombstone.state is ChatState.DELETED
        assert store.list_conversation(ChatMessage, "workspace-a", root.chat_id)
    finally:
        release.set()
        service._cleanup_queue.join()
    assert not store.list_conversation(SubAgentRecord, "workspace-a", root.chat_id)
    assert not store.list_conversation(ChatMessage, "workspace-a", root.chat_id)
    assert (
        store.get(Investigation, "workspace-a", artifact.investigation_id) == artifact
    )


def test_restart_resumes_durable_cleanup_without_stopping_deleted_executions() -> None:
    store = InMemoryChatPersistence()
    service = ChatService(store)
    root = service.create_chat("workspace-a", title="Delete", owner_actor_id="operator")
    service.create_message(
        "workspace-a", root.chat_id, actor_id="operator", body="Old history"
    )
    store.mark_conversation_deleted("workspace-a", root.chat_id, datetime.now(UTC))
    restarted = ChatService(store)
    assert restarted.stop_active_executions("workspace-a") == ()
    restarted.resume_conversation_cleanup("workspace-a")
    restarted._cleanup_queue.join()
    assert not store.list_conversation(ChatMessage, "workspace-a", root.chat_id)
