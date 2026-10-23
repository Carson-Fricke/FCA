"""API replicas and independent workers share durable intake and fenced outcomes."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from typing import Any, cast

import pytest
from azure.cosmos import ContainerProxy
from fastapi.testclient import TestClient
from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer, store_for

from thesis.application.composition import build_serving_stack, build_serving_worker
from thesis.http.application import create_app
from thesis_runtime.adapters.cosmos.execution_store import CosmosExecutionStore
from thesis_runtime.adapters.cosmos.ownership import CosmosOwnershipStore, lease_id
from thesis_runtime.adapters.cosmos.timeline import CosmosTimeline
from thesis_runtime.adapters.cosmos.work import CosmosWorkStore
from thesis_runtime.adapters.providers.invocations import ModelCompletion
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    from_record,
)
from thesis_runtime.contracts.output import OutputDelta
from thesis_runtime.contracts.records import (
    AgentInvocation,
    AgentInvocationState,
    ChatMessage,
    ChatState,
    ChatThread,
)
from thesis_runtime.conversations import ChatService, MessageRole
from thesis_runtime.execution.worker import Work
from thesis_runtime.storage.ownership import OwnershipLost, own


class Provider:
    model = "test/model"

    def __init__(self) -> None:
        self.calls = 0
        self.entered = Event()
        self.release = Event()
        self.release.set()

    def stream(self, *, message_id=None, **kwargs):
        from tests.thesis_runtime.provider_fakes import completion_frames

        yield OutputDelta("reasoning", "Working")
        yield from completion_frames(self.complete(**kwargs), message_id)

    streaming = True

    def complete(self, **kwargs: Any) -> ModelCompletion:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5)
        return ModelCompletion(
            content="Durable reply",
            reasoning="Working",
            model="test",
            provider="test",
            role=kwargs["role"],
            prompt_tokens=0,
            completion_tokens=0,
        )


def replica(container: LimitedContainer, provider: Provider):
    external = cast(ContainerProxy, container)
    ownership = CosmosOwnershipStore(external)
    chat = ChatService(
        persistence=store_for(container),
        ownership=ownership,
        single_process=False,
        timeline=CosmosTimeline(external),
    )
    work = CosmosWorkStore(external)
    stack = build_serving_stack(
        chat_service=chat, model_provider=cast(Any, provider), work_store=work
    )
    worker = build_serving_worker(
        stack, work, ownership, CosmosExecutionStore(external)
    )
    app = create_app(bearer_token="token", conversations=stack.conversations)
    return TestClient(app), chat, worker, work, ownership


def test_intake_survives_api_replacement_and_only_one_worker_replies() -> None:
    container, provider = LimitedContainer(), Provider()
    client, chat, _, work, _ = replica(container, provider)
    thread = chat.create_chat(
        "workspace", title="Distributed", owner_actor_id="operator"
    )
    response = client.post(
        f"/v1/workspaces/workspace/chats/{thread.chat_id}/messages",
        headers={"Authorization": "Bearer token"},
        json={"actor_id": "operator", "body": "Continue"},
    )
    assert response.status_code == 200, response.json()
    assert provider.calls == 0  # HTTP acceptance never executes a model.
    assert len(work.ready()) == 1
    _, restarted, worker, _, _ = replica(container, provider)
    _, _, rival, _, _ = replica(container, provider)
    provider.release.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(worker.tick)
        assert provider.entered.wait(5)
        assert rival.tick() == 0
        provider.release.set()
        result.result(timeout=5)
    assert rival.tick() == 0
    replies = [
        row
        for row in restarted.list_messages("workspace", thread.chat_id)
        if row.role is MessageRole.ASSISTANT
    ]
    assert [row.body for row in replies] == ["Durable reply"]
    assert provider.calls == 1
    assert restarted.read_consumed_input("workspace", thread.chat_id, "") > 0


def test_unexpected_root_fault_interrupts_durable_work_and_is_visible(caplog) -> None:
    class BrokenProvider(Provider):
        def stream(self, *, message_id=None, **kwargs):
            from tests.thesis_runtime.provider_fakes import completion_frames

            yield from completion_frames(self.complete(**kwargs), message_id)

        streaming = True

        def complete(self, **kwargs: Any) -> ModelCompletion:
            self.calls += 1
            raise RuntimeError("decoder broke")

    container, provider = LimitedContainer(), BrokenProvider()
    client, chat, worker, work, _ = replica(container, provider)
    thread = chat.create_chat("workspace", title="Fault", owner_actor_id="operator")
    response = client.post(
        f"/v1/workspaces/workspace/chats/{thread.chat_id}/messages",
        headers={"Authorization": "Bearer token"},
        json={"actor_id": "operator", "body": "Execute"},
    )
    assert response.status_code == 200, response.json()
    candidate = work.ready()[0]
    assert worker.tick() == 1
    assert work.get("workspace", candidate.key).state == "interrupted"
    durable = chat.list_messages("workspace", thread.chat_id)
    failures = [
        from_record(row).content.as_dict()["result"]
        for row in durable
        if "decoder broke" in row.body
    ]
    assert len(failures) == 1
    assert isinstance(failures[0], dict)
    assert failures[0]["failure"] == {
        "kind": "runtime_fault",
        "message": "decoder broke",
        "certainty": "unknown_outcome",
        "retry_safe": False,
    }
    assert not any(row.role is MessageRole.ASSISTANT for row in durable)
    assert "conversation turn failed" in caplog.text
    assert chat.read_consumed_input("workspace", thread.chat_id, "") == 0
    assert worker.tick() == 0
    assert provider.calls == 1


@pytest.mark.parametrize("sealed_partial", [False, True])
def test_expired_attempt_is_interrupted_without_replay_or_stale_output(
    sealed_partial: bool,
) -> None:
    container, provider = LimitedContainer(), Provider()
    _, chat, worker, work, _ = replica(container, provider)
    thread = chat.create_chat(
        "workspace", title="Interrupted", owner_actor_id="operator"
    )
    client, _, recovery, _, _ = replica(container, provider)
    response = client.post(
        f"/v1/workspaces/workspace/chats/{thread.chat_id}/messages",
        headers={"Authorization": "Bearer token"},
        json={"actor_id": "operator", "body": "Execute"},
    )
    assert response.status_code == 200, response.json()
    candidate = work.ready()[0]
    provider.release.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(worker.tick)
        assert provider.entered.wait(5)
        invocation = chat.persistence.list(AgentInvocation, "workspace")[0]
        if sealed_partial:
            # Simulate process loss after sealing output but before saving the
            # invocation's terminal state. Recovery must not infer success.
            partial = SessionMessage(
                "message-" + invocation.invocation_id,
                "outgoing",
                MessageContent(
                    "Model turn interrupted by new operator input.",
                    "model",
                    result={"interrupted": True, "reasoning": "Working"},
                ),
            )
            chat.create_message(
                "workspace",
                thread.chat_id,
                actor_id="model",
                body=partial.content.text,
                reasoning="Working",
                role=MessageRole.ASSISTANT,
                message_id=partial.message_id,
                data={"sessionMessage": partial.as_dict()},
            )
        container.items[("workspace", lease_id(candidate.resource))]["expiresAt"] = 0
        assert recovery.tick() == 1
        provider.release.set()
        result.result(timeout=5)
    assert work.get("workspace", candidate.key).state == "interrupted"
    assert provider.calls == 1
    replies = [
        row
        for row in chat.list_messages("workspace", thread.chat_id)
        if row.role is MessageRole.ASSISTANT
    ]
    assert len(replies) == int(sealed_partial)
    recovered = chat.persistence.list(AgentInvocation, "workspace")[0]
    assert recovered.state is AgentInvocationState.FAILED
    assert recovered.failure is not None
    assert recovered.failure["kind"] == (
        "operator_steering" if sealed_partial else "execution_interrupted"
    )
    assert recovered.provider_request == invocation.provider_request
    if sealed_partial:
        assert from_record(replies[0]).content.result == {
            "interrupted": True,
            "reasoning": "Working",
        }
    assert chat.read_consumed_input("workspace", thread.chat_id, "") == 0
    assert recovery.tick() == 0
    assert chat.timeline is not None
    progress = [
        cast(dict[str, object], row["payload"])
        for row in cast(CosmosTimeline, chat.timeline).read_after(
            workspace_id="workspace", chat_id=thread.chat_id
        )
        if row["kind"] == "output"
    ]
    assert any(row.get("text") == "Working" for row in progress)
    assert progress[-1]["state"] == "interrupted"


def test_stale_owner_cannot_publish_work_in_the_catalog_transaction() -> None:
    container = LimitedContainer()
    external = cast(ContainerProxy, container)
    ownership, work = CosmosOwnershipStore(external), CosmosWorkStore(external)
    claim = ownership.acquire("workspace", "turn", 60)
    assert claim is not None
    with pytest.raises(OwnershipLost), own(ownership, claim):
        container.items[("workspace", lease_id("turn"))]["expiresAt"] = 0
        replacement = ownership.acquire("workspace", "turn", 60)
        assert replacement is not None and replacement.fence > claim.fence
        work.enqueue(Work("workspace", "stale", "turn", "conversation", {}))
    with pytest.raises(KeyError):
        work.get("workspace", "stale")


def wait_completed(work: CosmosWorkStore, keys: tuple[str, ...]) -> None:
    deadline = monotonic() + 4
    while any(work.get("workspace", key).state != "completed" for key in keys):
        assert monotonic() < deadline, "retained notification was not dispatched"
        Event().wait(0.01)


def test_notification_dispatch_survives_api_restart_and_busy_conversation() -> None:
    container, provider = LimitedContainer(), Provider()
    client, chat, _, _, _ = replica(container, provider)
    thread = chat.create_chat("workspace", title="Feed", owner_actor_id="operator")
    url = f"/v1/workspaces/workspace/chats/{thread.chat_id}/messages"
    headers = {"Authorization": "Bearer token"}
    first = client.post(
        url, headers=headers, json={"actor_id": "operator", "body": "First"}
    )
    assert first.status_code == 200, first.json()
    assert not container.queries  # Admission uses only point reads and a batch.
    _, restarted, worker, work, _ = replica(container, provider)
    provider.release.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(worker.run)
        try:
            assert provider.entered.wait(2)
            second = client.post(
                url,
                headers=headers,
                json={"actor_id": "operator", "body": "While busy"},
            )
            assert second.status_code == 200, second.json()
            second_key = "input:" + second.json()["message_id"]
            deadline = monotonic() + 2
            while second_key not in container.feed_keys:
                assert monotonic() < deadline
                Event().wait(0.01)
            provider.release.set()
            keys = tuple(
                "input:" + item.json()["message_id"] for item in (first, second)
            )
            wait_completed(work, keys)
            assert provider.calls == 2
            assert (
                restarted.read_consumed_input("workspace", thread.chat_id, "")
                == second.json()["input_position"]
            )
            assert container.feed_reads > 0
            assert (
                len([query for query in container.queries if "RuntimeWork" in query])
                <= 2
            )  # Bounded startup snapshot, repeated if it raced feed initialization.
            # One input transaction owns each runnable create. Scheduling must
            # not try a duplicate create after admission.
            creates = [
                args[0]
                for batch in container.batches
                for verb, args, *_ in batch
                if verb == "create" and args[0].get("recordType") == "RuntimeWork"
            ]
            assert len(creates) == 2
        finally:
            provider.release.set()
            worker.stop()
            running.result(timeout=5)


def test_slow_retained_work_scan_does_not_block_live_dispatch() -> None:
    class SlowReconciliation(LimitedContainer):
        entered = Event()
        release = Event()

        def query_items(self, *, query: str, **kwargs: Any):
            if "c.recordType = 'RuntimeWork'" in query:
                self.entered.set()
                assert self.release.wait(5)
            return super().query_items(query=query, **kwargs)

    container, provider = SlowReconciliation(), Provider()
    client, chat, worker, work, _ = replica(container, provider)
    thread = chat.create_chat(
        "workspace", title="Independent dispatch", owner_actor_id="operator"
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(worker.run)
        try:
            assert container.entered.wait(2)
            response = client.post(
                f"/v1/workspaces/workspace/chats/{thread.chat_id}/messages",
                headers={"Authorization": "Bearer token"},
                json={"actor_id": "operator", "body": "Continue during recovery"},
            )
            assert response.status_code == 200, response.json()
            assert provider.entered.wait(2), "recovery scan blocked new input"
            wait_completed(work, ("input:" + response.json()["message_id"],))
            assert not container.release.is_set()
            assert provider.calls == 1
        finally:
            container.release.set()
            worker.stop()
            running.result(timeout=5)


def test_failed_admission_leaves_no_message_cursor_or_runnable_work() -> None:
    class RejectWork(LimitedContainer):
        reject = True

        def _store(self, body: dict[str, Any]) -> dict[str, Any]:
            if self.reject and body.get("recordType") == "RuntimeWork":
                raise RuntimeError("transaction failed")
            return super()._store(body)

    container = RejectWork()
    client, chat, _, work, _ = replica(container, Provider())
    client = TestClient(client.app, raise_server_exceptions=False)
    thread = chat.create_chat("workspace", title="Atomic", owner_actor_id="operator")
    url = f"/v1/workspaces/workspace/chats/{thread.chat_id}/messages"
    headers = {"Authorization": "Bearer token"}
    response = client.post(
        url, headers=headers, json={"actor_id": "operator", "body": "Retain together"}
    )
    assert response.status_code == 500
    assert not any(
        row.get("recordType") in {"ChatMessage", "ConversationInbox", "RuntimeWork"}
        for row in container.items.values()
    )
    assert ("workspace", "counter:" + thread.chat_id) not in container.items
    assert chat.get_chat("workspace", thread.chat_id) == thread
    container.reject = False
    response = client.post(
        url, headers=headers, json={"actor_id": "operator", "body": "Retain together"}
    )
    assert response.status_code == 200, response.json()
    assert response.json()["input_position"] == 1
    assert (
        work.get("workspace", "input:" + response.json()["message_id"]).state
        == "pending"
    )


@pytest.mark.parametrize(
    "consumed", [True, False], ids=["consumed-history", "pending-outside-tail"]
)
def test_bounded_session_history_and_unchanged_head_need_no_catalog_scan(
    consumed: bool,
) -> None:
    class BoundedProvider(Provider):
        def stream(self, *, message_id=None, **kwargs):
            from tests.thesis_runtime.provider_fakes import completion_frames

            yield from completion_frames(self.complete(**kwargs), message_id)

        streaming = True

        def complete(self, **kwargs: Any) -> ModelCompletion:
            messages = kwargs["messages"]
            assert len(messages) == (
                41 if consumed else 42
            )  # Policy, tail, and any pending input outside the tail.
            assert "Continue" in messages[-1].content
            assert (
                any("oldest input" in message.content for message in messages)
                is not consumed
            )
            return super().complete(**kwargs)

    container, provider = LimitedContainer(), BoundedProvider()
    client, chat, worker, work, _ = replica(container, provider)
    thread = chat.create_chat("workspace", title="Window", owner_actor_id="operator")
    initial = chat.send_message(
        "workspace", thread.chat_id, actor_id="operator", body="oldest input"
    )
    for index in range(80):
        chat.create_message(
            "workspace",
            thread.chat_id,
            actor_id="model",
            body=f"History {index}",
            role=MessageRole.ASSISTANT,
        )
    if consumed:
        chat.acknowledge_input("workspace", thread.chat_id, "", initial.input_position)
    container.queries.clear()
    response = client.post(
        f"/v1/workspaces/workspace/chats/{thread.chat_id}/messages",
        headers={"Authorization": "Bearer token"},
        json={"actor_id": "operator", "body": "Continue"},
    )
    assert response.status_code == 200, response.json()
    assert not container.queries
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(worker.run)
        try:
            wait_completed(work, ("input:" + response.json()["message_id"],))
        finally:
            worker.stop()
            running.result(timeout=5)
    assert provider.calls == 1
    assert (
        len(container.queries) == 3
    )  # Startup work snapshot, initial tail, pending inputs.
    for _ in range(3):
        assert (
            chat.read_message_window(
                "workspace",
                thread.chat_id,
                thread.chat_id,
                after_position=response.json()["input_position"],
            )
            == ()
        )
    assert len(container.queries) == 3


class ConcurrentCatalog(LimitedContainer):
    """Run a competing external transaction immediately before the next commit."""

    before_commit: Any = None
    lose_response = False
    fail_before_commit = False

    def execute_item_batch(
        self, *, batch_operations: list[Any], partition_key: str
    ) -> None:
        callback, self.before_commit = self.before_commit, None
        if callback is not None:
            callback()
        if self.fail_before_commit:
            self.fail_before_commit = False
            raise TimeoutError("commit outcome unknown")
        super().execute_item_batch(
            batch_operations=batch_operations, partition_key=partition_key
        )
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("commit response lost")


def post_input(client: TestClient, chat_id: str, identity: str, body: str = "Continue"):
    return client.post(
        f"/v1/workspaces/workspace/chats/{chat_id}/messages",
        headers={"Authorization": "Bearer token"},
        json={"actor_id": "operator", "body": body, "message_id": identity},
    )


def test_plain_send_is_one_conditional_transaction_and_retries_join_identity() -> None:
    container = LimitedContainer()
    client, chat, _, work, _ = replica(container, Provider())
    thread = chat.create_chat(
        "workspace", title="Fast admission", owner_actor_id="operator"
    )
    container.reads.clear()
    container.batches.clear()
    response = post_input(client, thread.chat_id, "message-stable")
    assert response.status_code == 200, response.json()
    assert sorted(container.reads) == sorted(
        ["counter:" + thread.chat_id, "ChatThread:" + thread.chat_id]
    )
    assert len(container.batches) == 1
    assert not any(
        row.get("recordType") == "Ownership" for row in container.items.values()
    )
    rival, _, _, _, _ = replica(container, Provider())
    duplicate = post_input(rival, thread.chat_id, "message-stable")
    assert duplicate.status_code == 200 and duplicate.json() == response.json()
    conflict = post_input(rival, thread.chat_id, "message-stable", "Changed")
    assert conflict.status_code == 422
    assert (
        len(chat.list_messages("workspace", thread.chat_id)) == len(work.ready()) == 1
    )


@pytest.mark.parametrize("mutation", ["send", "ack", "references", "archive", "delete"])
def test_admission_recomputes_after_competing_root_mutation(mutation: str) -> None:
    container = ConcurrentCatalog()
    client, chat, _, work, _ = replica(container, Provider())
    rival, other, _, _, _ = replica(container, Provider())
    thread = chat.create_chat(
        "workspace", title="Concurrent admission", owner_actor_id="operator"
    )
    initial = post_input(client, thread.chat_id, "message-initial").json()

    def mutate() -> None:
        if mutation == "send":
            assert post_input(rival, thread.chat_id, "message-rival").status_code == 200
        elif mutation == "ack":
            other.acknowledge_input(
                "workspace", thread.chat_id, "", initial["input_position"]
            )
            other.acknowledge_input(
                "workspace", thread.chat_id, "child", initial["input_position"]
            )
        elif mutation == "references":
            other.link_resources(
                "workspace", thread.chat_id, {"example.document": ("reference",)}
            )
        elif mutation == "archive":
            other.archive_chat("workspace", thread.chat_id)
        else:
            other.delete_chat("workspace", thread.chat_id)

    container.before_commit = mutate
    response = post_input(client, thread.chat_id, "message-racing")
    if mutation in {"archive", "delete"}:
        assert response.status_code == (409 if mutation == "archive" else 404), (
            response.json()
        )
        with pytest.raises(KeyError):
            work.get("workspace", "input:message-racing")
        assert chat.persistence.get(ChatMessage, "workspace", "message-racing") is None
    else:
        assert response.status_code == 200, response.json()
        root = other.get_chat("workspace", thread.chat_id)
        if mutation == "ack":
            assert root.input_consumed_positions == {
                "": initial["input_position"],
                "child": initial["input_position"],
            }
        elif mutation == "references":
            assert root.links.references["example.document"] == ("reference",)
        else:
            assert [
                row.message_id
                for row in chat.list_messages("workspace", thread.chat_id)
            ] == ["message-initial", "message-rival", "message-racing"]
        assert response.json()["input_position"] > initial["input_position"]


@pytest.mark.parametrize("mutation", ["ack", "references", "archive", "delete"])
def test_root_mutation_recomputes_after_competing_admission(mutation: str) -> None:
    container = ConcurrentCatalog()
    client, chat, _, _, _ = replica(container, Provider())
    rival, other, _, _, _ = replica(container, Provider())
    thread = chat.create_chat(
        "workspace", title="Concurrent update", owner_actor_id="operator"
    )
    first = post_input(client, thread.chat_id, "message-first").json()
    container.before_commit = lambda: post_input(
        rival, thread.chat_id, "message-before-update"
    )
    if mutation == "ack":
        chat.acknowledge_input("workspace", thread.chat_id, "", first["input_position"])
    elif mutation == "references":
        chat.link_resources(
            "workspace", thread.chat_id, {"example.document": ("reference",)}
        )
    elif mutation == "archive":
        chat.archive_chat("workspace", thread.chat_id)
    else:
        chat.delete_chat("workspace", thread.chat_id)
    root = other.persistence.get(ChatThread, "workspace", thread.chat_id)
    assert root is not None
    if mutation == "delete":
        assert root.state is ChatState.DELETED
    else:
        assert len(other.list_messages("workspace", thread.chat_id)) == 2
        if mutation == "ack":
            assert root.input_consumed_positions == {"": first["input_position"]}
        elif mutation == "references":
            assert root.links.references["example.document"] == ("reference",)
        else:
            assert root.state is ChatState.ARCHIVED


@pytest.mark.parametrize("committed", [True, False])
def test_uncertain_admission_reconciles_identity_without_replaying(
    committed: bool,
) -> None:
    container = ConcurrentCatalog()
    original, chat, _, work, _ = replica(container, Provider())
    client = TestClient(original.app, raise_server_exceptions=False)
    thread = chat.create_chat(
        "workspace", title="Unknown outcome", owner_actor_id="operator"
    )
    container.batches.clear()
    container.lose_response = committed
    container.fail_before_commit = not committed
    response = post_input(client, thread.chat_id, "message-uncertain")
    assert response.status_code == (200 if committed else 500)
    assert len(container.batches) == (1 if committed else 0)
    assert (
        len(chat.list_messages("workspace", thread.chat_id))
        == len(work.ready())
        == int(committed)
    )
    if committed:
        duplicate = post_input(client, thread.chat_id, "message-uncertain")
        assert duplicate.json() == response.json()
        assert len(work.ready()) == 1
