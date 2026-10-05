"""Opt-in real Cosmos acceptance, with no model calls or workspace creation.

Run from the repository root with existing Cosmos environment settings (the
local emulator is the default when no endpoint is configured):
    $env:FCA_LIVE_COSMOS_LATENCY = '1'
    uv run --extra dev pytest -p no:cacheprovider -s --tb=short \
        tests/thesis/cosmos/test_conversation_latency_live.py

The HTTP client is in-process, but uses the authenticated production app and
real Cosmos storage. Timings include the complete request/response processing,
not app construction or seeding. They exclude external HTTP/TLS and browser
rendering. No artifact storage is opened or modified; artifact preservation is
not verified. Cleanup retains durable root tombstones by design. The 30-second
purge polling deadline cannot interrupt an in-flight Cosmos SDK request.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from time import monotonic, perf_counter, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from thesis.agent.runtime.datum import tool_result_payload
from thesis.chat.contracts import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
    ChatContractError,
    ChatMessage,
    ChatState,
    ChatThread,
    MessageRole,
    RecordNotFound,
    SubAgentRecord,
    TranscriptItemKind,
)
from thesis.cosmos.client import CosmosSettings
from thesis.cosmos.stack import open_cosmos_chat_stack
from thesis.http.application import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("FCA_LIVE_COSMOS_LATENCY") != "1",
    reason="real Cosmos latency acceptance requires FCA_LIVE_COSMOS_LATENCY=1",
)


def test_real_cosmos_conversation_latency() -> None:
    """Every list/open/delete must take <1s during unrelated durable writes."""
    settings = CosmosSettings.from_environment()
    workspace = os.environ.get("FCA_LIVE_COSMOS_LATENCY_WORKSPACE", "test-ws-1")
    assert workspace in {"test-ws-1", "latency-test"}, (
        "Use existing test-ws-1 or the dedicated latency-test workspace identifier"
    )
    chat, timeline = open_cosmos_chat_stack(settings)
    store = chat.persistence
    run_id = f"latency-{uuid4().hex}"
    token = uuid4().hex  # Local HTTP authentication only, never a Cosmos credential.
    app = create_app(
        bearer_token=token, chat_service=chat, timeline=timeline, in_memory=False
    )
    roots: list[ChatThread] = []
    timings: list[tuple[str, int, float]] = []
    saved_records: list[ChatMessage | SubAgentRecord | AgentInvocation] = []
    base = f"/v1/workspaces/{workspace}/chats"

    def create_root(label: str) -> ChatThread:
        root = chat.create_chat(
            workspace, title=f"{run_id}-{label}", owner_actor_id=run_id
        )
        roots.append(root)
        return root

    def report(operation: str, repeat: int, started: float) -> None:
        elapsed = perf_counter() - started
        timings.append((operation, repeat, elapsed))
        print(f"cosmos_latency op={operation} repeat={repeat} seconds={elapsed:.6f}")

    try:
        other = create_root("concurrent")
        batch = [create_root(f"root-{i}") for i in range(3)]
        expected_messages: dict[str, set[str]] = {}
        for root in batch:
            parent: str | None = None
            children: list[SubAgentRecord] = []
            for depth in range(1, 4):
                child = store.save(
                    SubAgentRecord(
                        workspace_id=workspace,
                        created_at=datetime.now(UTC),
                        created_by=run_id,
                        chat_id=root.chat_id,
                        sub_agent_id=f"{run_id}-{root.chat_id}-{depth}",
                        role="latency-helper",
                        parent_sub_agent_id=parent,
                    )
                )
                saved_records.append(child)
                children.append(child)
                parent = child.sub_agent_id
            expected_messages[root.chat_id] = set()
            # Exactly thirty messages per representative root: 21 root and
            # three at each nested node, including canonical tool outcomes.
            for child in [None, *children]:
                for index in range(21 if child is None else 3):
                    tool = index % 3 == 2
                    call_id = f"call-{uuid4().hex}"
                    message = chat.send_message(
                        workspace,
                        root.chat_id,
                        actor_id=run_id,
                        body=f"{run_id} representative message {index}",
                        role=MessageRole.TOOL if tool else MessageRole.USER,
                        kind=TranscriptItemKind.TOOL
                        if tool
                        else TranscriptItemKind.MESSAGE,
                        tool_result=tool_result_payload(
                            call_id=call_id,
                            ok=True,
                            failure=None,
                            structured={"value": index},
                            summary="Latency fixture outcome",
                            text="Completed",
                            tool="latency_fixture_tool",
                            datum_type="tool_result",
                        )
                        if tool
                        else None,
                        data={"subAgentId": child.sub_agent_id} if child else None,
                    )
                    saved_records.append(message)
                    expected_messages[root.chat_id].add(message.message_id)
                    if tool:
                        invocation = store.save(
                            AgentInvocation(
                                workspace_id=workspace,
                                created_at=datetime.now(UTC),
                                created_by=run_id,
                                chat_id=root.chat_id,
                                conversation_id=child.sub_agent_id
                                if child
                                else root.chat_id,
                                parent_conversation_id=child.parent_conversation_id
                                if child
                                else None,
                                sub_agent_id=child.sub_agent_id if child else None,
                                invocation_id=f"{run_id}-{message.message_id}",
                                kind=AgentInvocationKind.TOOL,
                                state=AgentInvocationState.SUCCEEDED,
                                tool_name="latency_fixture_tool",
                                tool_request_id=call_id,
                                idempotency_key=f"{run_id}-{message.message_id}",
                            )
                        )
                        saved_records.append(invocation)

        stop = Event()
        written = Event()

        def append_other() -> int:
            count = 0
            while not stop.is_set():
                chat.send_message(
                    workspace,
                    other.chat_id,
                    actor_id=run_id,
                    body=f"{run_id} concurrent append {count}",
                )
                count += 1
                written.set()
                stop.wait(0.05)
            return count

        with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
            # Seeding is excluded; join the writer before deleting its root.
            with ThreadPoolExecutor(max_workers=1) as pool:
                writer = pool.submit(append_other)
                try:
                    assert written.wait(30), "concurrent Cosmos writer did not start"
                    for repeat in range(1, 6):
                        assert not writer.done()
                        started = perf_counter()
                        listed = chat.list_chats(workspace)
                        report("list_chats", repeat, started)
                        assert {root.chat_id for root in batch} <= {
                            root.chat_id for root in listed
                        }
                        started = perf_counter()
                        response = client.get(base)
                        report("http_list_chats", repeat, started)
                        assert response.status_code == 200
                        assert {root.chat_id for root in batch} <= {
                            item["chat_id"] for item in response.json()["items"]
                        }
                        for root in batch:
                            assert not writer.done(), "concurrent writer stopped early"
                            started = perf_counter()
                            response = client.get(f"{base}/{root.chat_id}/timeline")
                            report("http_timeline_open", repeat, started)
                            assert response.status_code == 200
                            assert expected_messages[root.chat_id] == {
                                item["payload"]["message_id"]
                                for item in response.json()["items"]
                                if item["kind"] == "message"
                            }
                    for deletion, root in enumerate(batch, start=1):
                        assert not writer.done(), "concurrent writer stopped early"
                        started = perf_counter()
                        response = client.delete(f"{base}/{root.chat_id}")
                        report("http_logical_delete", deletion, started)
                        assert response.status_code == 204
                        assert (
                            client.get(f"{base}/{root.chat_id}/timeline").status_code
                            == 404
                        )
                        with pytest.raises(RecordNotFound, match="deleted"):
                            chat.send_message(
                                workspace,
                                root.chat_id,
                                actor_id=run_id,
                                body="late",
                            )
                        # Exercise persistence admission for root, child,
                        # message and invocation writes, not only HTTP intake.
                        with pytest.raises(ChatContractError, match="deleted"):
                            store.save(replace(root, title="attempted resurrection"))
                        for record_type in (
                            ChatMessage,
                            SubAgentRecord,
                            AgentInvocation,
                        ):
                            record = next(
                                record
                                for record in saved_records
                                if record.chat_id == root.chat_id
                                and isinstance(record, record_type)
                            )
                            with pytest.raises(ChatContractError, match="deleted"):
                                store.save(record)
                    assert not {root.chat_id for root in batch} & {
                        root.chat_id for root in chat.list_chats(workspace)
                    }
                    assert (
                        chat.get_chat(workspace, other.chat_id).state
                        is ChatState.ACTIVE
                    )
                finally:
                    stop.set()
                    count = writer.result(timeout=30)
                    print(f"cosmos_latency concurrent_appends={count}")

            assert len(timings) == 28
            violations = [
                f"{operation} repeat={repeat}: {elapsed:.6f}s"
                for operation, repeat, elapsed in timings
                if elapsed >= 1.0
            ]
            assert not violations, "Expected every operation <1 second: " + "; ".join(
                violations
            )
    finally:
        # Never resume workspace-wide cleanup or directly purge shared storage.
        # Only the service's background worker physically removes owned records.
        for root in roots:
            chat.delete_chat(workspace, root.chat_id)
        deadline = monotonic() + 30
        pending = {root.chat_id for root in roots}
        while pending and monotonic() < deadline:
            for chat_id in tuple(pending):
                if (
                    all(
                        not store.list_conversation(record_type, workspace, chat_id)
                        for record_type in (
                            ChatMessage,
                            SubAgentRecord,
                            AgentInvocation,
                        )
                    )
                    and not timeline.read_after(
                        workspace_id=workspace, chat_id=chat_id, limit=1
                    )
                    and timeline.latest_sequence(
                        workspace_id=workspace, chat_id=chat_id
                    )
                    == 0
                ):
                    pending.remove(chat_id)
            if pending:
                sleep(min(0.1, max(0, deadline - monotonic())))
        assert not pending, (
            f"Owned catalog/timeline records not purged within 30s: {sorted(pending)}"
        )
        for root in roots:
            tombstone = store.get(ChatThread, workspace, root.chat_id)
            assert tombstone is not None and tombstone.state is ChatState.DELETED
