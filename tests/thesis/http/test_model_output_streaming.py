"""Live model output through real provider/session/Cosmos/HTTP composition."""

import json
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from typing import cast

import httpx
import pytest
from azure.cosmos import ContainerProxy
from fastapi.testclient import TestClient
from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer

from thesis.http.application import create_app
from thesis_runtime.adapters.cosmos.notifications import CosmosTimelineNotifications
from thesis_runtime.adapters.cosmos.records import CosmosChatPersistence
from thesis_runtime.adapters.cosmos.timeline import CosmosTimeline
from thesis_runtime.adapters.providers.openrouter import (
    OpenRouterProfile,
    OpenRouterProvider,
)
from thesis_runtime.adapters.providers.session_adapter import NativeSessionAdapter
from thesis_runtime.adapters.providers.transports import HttpOpenRouterTransport
from thesis_runtime.contracts.records import (
    AgentInvocation,
    AgentInvocationState,
    SubAgentKind,
    WorkflowExecution,
    WorkflowExecutionState,
)
from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.delivery import Anchor
from thesis_runtime.subagents.specs import SubAgentSpec
from thesis_runtime.tools.registry import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)


class Key:
    def key(self) -> str:
        return "test-key"


def frame(delta: dict[str, object], finish: str | None = None) -> bytes:
    return (
        "data: "
        + json.dumps(
            {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]},
            ensure_ascii=False,
        )
        + "\n\n"
    ).encode()


def test_http_steering_closes_provider_and_retains_reasoning_in_next_request() -> None:
    ready, release, closed = Event(), Event(), Event()
    requests: list[dict] = []
    queued: list[Callable[[], None]] = []
    dispatched: list[object] = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield frame(
                {
                    "reasoning": "Preserved thought \U0001f9e0",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "obsolete",
                            "function": {"name": "work", "arguments": '{"unfinished":'},
                        }
                    ],
                }
            )
            ready.set()
            assert release.wait(2)
            Event().wait(0.26)
            yield frame({"reasoning": " including its tail"})
            pytest.fail("the steered HTTP stream was not closed")

        def close(self):
            closed.set()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, stream=Body())
        assert len(requests) == 2 and closed.is_set()
        assert any(
            "Preserved thought \U0001f9e0 including its tail" in str(item)
            for item in requests[-1]["messages"]
        )
        assert any("New steering" in str(item) for item in requests[-1]["messages"])
        return httpx.Response(
            200, content=frame({"content": "New answer"}, "stop") + b"data: [DONE]\n\n"
        )

    container = cast(ContainerProxy, LimitedContainer())
    timeline = CosmosTimeline(container)
    chat = ChatService(
        CosmosChatPersistence(container, timeline=timeline), timeline=timeline
    )
    root = chat.create_chat("workspace", title="Steering", owner_actor_id="operator")

    def work(arguments, context):
        dispatched.append(arguments)
        return ToolExecution(ok=True)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        provider = OpenRouterProvider(
            OpenRouterProfile(profile_id="test", model="test/model"),
            Key(),
            HttpOpenRouterTransport("https://provider.test", client=http),
        )
        conversations = Conversations(
            chat=chat,
            provider=NativeSessionAdapter(provider),
            spawn_thread=queued.append,
            registry_factory=lambda _: SessionToolRegistry(
                [RegisteredTool({"name": "work"}, work)]
            ),
        )
        with TestClient(
            create_app(
                bearer_token="token", conversations=conversations, timeline=timeline
            )
        ) as client:
            headers = {"Authorization": "Bearer token"}
            base = f"/v1/workspaces/workspace/chats/{root.chat_id}"
            assert (
                client.post(
                    base + "/messages",
                    headers=headers,
                    json={"body": "Question", "actor_id": "operator"},
                ).status_code
                == 200
            )
            worker = Thread(target=queued.pop(), daemon=True)
            worker.start()
            try:
                assert ready.wait(1)
                assert (
                    client.post(
                        base + "/messages",
                        headers=headers,
                        json={"body": "New steering", "actor_id": "operator"},
                    ).status_code
                    == 200
                )
                assert chat.read_consumed_input("workspace", root.chat_id, "") == 0
            finally:
                release.set()
                worker.join(2)
            assert not worker.is_alive() and closed.is_set()
            rows = client.get(base + "/timeline?after=0", headers=headers).json()[
                "items"
            ]
            invocations = chat.persistence.list(AgentInvocation, "workspace")
            assert [item.state for item in invocations] == [
                AgentInvocationState.FAILED,
                AgentInvocationState.SUCCEEDED,
            ]
            assert invocations[0].failure is not None
            assert invocations[0].failure["kind"] == "operator_steering"
            outputs = [
                item["payload"]
                for item in rows
                if item["kind"] == "message" and item["payload"]["role"] == "assistant"
            ]
            assert len(outputs) == 2
            assert (
                outputs[0]["reasoning"]
                == "Preserved thought \U0001f9e0 including its tail"
            )
            assert outputs[1]["body"] == "New answer"
            assert dispatched == []
            assert len(requests) == 2


@pytest.mark.parametrize(
    "outcome", ["complete", "midstream-error", "output-write-failure"]
)
@pytest.mark.parametrize("scope", ["root", "helper", "phase"])
def test_reasoning_is_durable_and_streamed_before_provider_finishes(
    outcome: str,
    scope: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("thesis_runtime.sessions.recovery.sleep", lambda _: None)
    failed = outcome != "complete"
    release = Event()
    emitted = Event()
    closed = Event()

    class Body(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            # Split UTF-8 and SSE framing arbitrarily; comments are not output.
            data = b": processing\n\n" + frame(
                {
                    "reasoning": "Thinking \U0001f9e0",
                    **(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "work-call",
                                    "function": {
                                        "name": "work",
                                        "arguments": '{"value":',
                                    },
                                }
                            ]
                        }
                        if failed
                        else {}
                    ),
                    "reasoning_details": [
                        {"type": "reasoning.text", "text": "duplicate"}
                    ],
                }
            )
            for index in range(0, len(data), 7):
                yield data[index : index + 7]
            # The second small fragment must flush even while the socket is idle.
            yield frame(
                {
                    "reasoning_details": [
                        {"type": "reasoning.summary", "summary": " now"},
                        {"type": "reasoning.encrypted", "data": "hidden"},
                    ],
                    **(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": None,
                                    "function": {"name": None, "arguments": "1}"},
                                }
                            ]
                        }
                        if failed
                        else {}
                    ),
                }
            )
            emitted.set()
            assert release.wait(2), "test did not release provider completion"
            if outcome == "midstream-error":
                yield b'data: {"error":{"message":"upstream failed"}}\n\n'
            else:
                yield frame({"content": "Answer"}, "stop")
                yield b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n'
                yield b"data: [DONE]\n\n"

        def close(self) -> None:
            closed.set()

    class Container(LimitedContainer):
        def execute_item_batch(self, *, batch_operations, partition_key):
            if outcome == "output-write-failure" and any(
                operation[0] == "create"
                and operation[1][0].get("recordType") == "ChatMessage"
                and operation[1][0].get("payload", {}).get("role") == "assistant"
                for operation in batch_operations
            ):
                raise RuntimeError("output storage unavailable")
            return super().execute_item_batch(
                batch_operations=batch_operations, partition_key=partition_key
            )

    container = cast(ContainerProxy, Container())
    writer = CosmosTimeline(container)
    notifications = CosmosTimelineNotifications(container)
    reader = CosmosTimeline(container, notifier=notifications)
    chat = ChatService(
        persistence=CosmosChatPersistence(container, timeline=writer), timeline=writer
    )
    root = chat.create_chat("workspace", title="Streaming", owner_actor_id="operator")
    queued: list[Callable[[], None]] = []
    dispatched: list[object] = []

    def work(arguments, context):
        dispatched.append(arguments)
        return ToolExecution(ok=True, structured={})

    registry = SessionToolRegistry(
        [
            RegisteredTool(
                {
                    "name": "work",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                    },
                },
                work,
            )
        ]
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Body()))
    ) as http:
        provider = OpenRouterProvider(
            OpenRouterProfile(profile_id="test", model="test/model"),
            Key(),
            HttpOpenRouterTransport("https://provider.test", client=http),
        )
        conversations = Conversations(
            chat=chat,
            provider=NativeSessionAdapter(provider),
            spawn_thread=queued.append,
            registry_factory=lambda _: registry,
        )
        anchor_id = ""
        if scope != "root":
            parent = Anchor("workspace", root.chat_id)
            attribution = None
            if scope == "phase":
                chat.persistence.save(
                    WorkflowExecution(
                        workspace_id="workspace",
                        created_at=root.created_at,
                        created_by="operator",
                        execution_id="execution",
                        chat_id=root.chat_id,
                        trigger_message_id="trigger",
                        workflow_id="research",
                        workflow_version="1.0.0",
                        requested_by="operator",
                        state=WorkflowExecutionState.RUNNING,
                        conversation_id=root.chat_id,
                        root_conversation_id=root.chat_id,
                        parent_conversation_id=None,
                    )
                )
                owner = conversations.lifecycle.spawn(
                    "workspace",
                    root.chat_id,
                    spec=SubAgentSpec(role="workflow-node", allowed_tools=()),
                    actor_id="operator",
                    workflow_execution_id="execution",
                )
                parent = Anchor("workspace", root.chat_id, owner.sub_agent_id)
                attribution = {
                    "workflow_execution_id": "execution",
                    "phase_attempt_id": "attempt",
                }
            node = conversations.spawn(
                parent,
                SubAgentSpec(
                    role="research",
                    phase="analysis" if scope == "phase" else None,
                    allowed_tools=("work",),
                ),
                "Initial question",
                SubAgentKind.CONVERSATION,
                phase_executor=scope == "phase",
                attribution=attribution,
            )
            anchor_id = node.conversation_id
        # Reader composition is independent of the worker's local notifier.
        client = TestClient(
            create_app(
                bearer_token="token",
                conversations=conversations,
                timeline=reader,
                stream_max_poll_cycles=1,
            )
        )
        chat.timeline = writer
        headers = {"Authorization": "Bearer token"}
        base = f"/v1/workspaces/workspace/chats/{root.chat_id}"
        receipt = client.post(
            base + (f"/sub_agents/{anchor_id}" if anchor_id else "") + "/messages",
            headers=headers,
            json={
                "body": "Question",
                **({"actor_id": "operator"} if not anchor_id else {}),
            },
        )
        assert receipt.status_code == 200
        unsubscribe = notifications.subscribe("workspace", root.chat_id)
        version = notifications.version("workspace", root.chat_id)
        # Observe one bounded attempt. The serialized worker now correctly keeps
        # retrying an unavailable provider; this test inspects streaming evidence,
        # while dedicated recovery tests execute the scheduled continuation.
        errors = []

        def run_slice():
            try:
                conversations.run_node_turn("workspace", root.chat_id, anchor_id)
            except Exception as error:
                errors.append(error)

        worker = Thread(target=run_slice, daemon=True)
        worker.start()
        try:
            assert emitted.wait(1)
            assert notifications.wait("workspace", root.chat_id, version, 1) != version
            # Wait on commit notifications until the timed batch is committed.
            for _ in range(4):
                response = client.get(
                    base + "/timeline/stream?after=0", headers=headers
                )
                rows = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                thoughts = [
                    row["payload"]["text"]
                    for row in rows
                    if row["kind"] == "output" and row["payload"]["state"] == "delta"
                ]
                if " now" in thoughts:
                    break
                current = notifications.version("workspace", root.chat_id)
                notifications.wait("workspace", root.chat_id, current, 0.3)
            assert "".join(thoughts) == "Thinking \U0001f9e0 now"
            assert not closed.is_set()
            assert dispatched == []
            assert chat.read_consumed_input("workspace", root.chat_id, anchor_id) == 0
            invocation = chat.persistence.list(AgentInvocation, "workspace")[0]
            assert invocation.state is AgentInvocationState.STARTED
            assert invocation.provider_request is not None
            assert invocation.provider_request["stream"] is True
            output_id = next(
                row["payload"]["messageId"] for row in rows if row["kind"] == "output"
            )
            assert all(
                row.get("threadId") == (anchor_id or "root")
                for row in rows
                if row["kind"] == "output"
            )
            if scope == "phase":
                assert all(
                    row.get("executionId") == "execution"
                    for row in rows
                    if row["kind"] == "output"
                )
        finally:
            release.set()
            worker.join(2)
            unsubscribe()
            notifications.close()
        assert not worker.is_alive()
        if outcome != "output-write-failure":
            assert errors == []
        elif scope == "root":
            assert errors
        assert closed.is_set()
        assert dispatched == []
        final = client.get(base + "/timeline?after=0", headers=headers).json()["items"]
        outputs = [
            row
            for row in final
            if row["kind"] == "message" and row["payload"]["role"] == "assistant"
        ]
        invocation = chat.persistence.list(AgentInvocation, "workspace")[0]
        if failed:
            if outcome == "midstream-error":
                # Helpers/phases have both a birth prompt and this HTTP input;
                # unacknowledged queued wakes may each exhaust a bounded turn.
                assert 4 <= len(outputs) <= (4 if scope == "root" else 8)
                assert outputs[0]["payload"]["message_id"] == output_id
                for output in outputs:
                    detail = client.get(
                        base + f"/timeline/items/{output['seq']}", headers=headers
                    )
                    assert detail.status_code == 200
                    canonical = detail.json()["payload"]["data"]["sessionMessage"]
                    assert canonical["content"]["result"]["interrupted"] is True
                    assert (
                        canonical["content"]["result"]["reasoning"]
                        == "Thinking \U0001f9e0 now"
                    )
                    assert not canonical["content"]["requests"]
            else:
                assert not outputs
            assert invocation.state is AgentInvocationState.FAILED
            assert chat.read_consumed_input("workspace", root.chat_id, anchor_id) == 0
            assert any(
                row["kind"] == "output" and row["payload"]["state"] == "interrupted"
                for row in final
            )
        else:
            assert (
                len(outputs) == 1 and outputs[0]["payload"]["message_id"] == output_id
            )
            assert outputs[0]["payload"]["reasoning"] == "Thinking \U0001f9e0 now"
            assert outputs[0]["payload"]["body"] == "Answer"
            assert invocation.state is AgentInvocationState.SUCCEEDED
            assert chat.read_consumed_input("workspace", root.chat_id, anchor_id) > 0


def test_separate_api_wakes_only_for_its_chat_before_recovery_polling() -> None:
    queried = Event()
    unrelated_read = Event()

    class Container(LimitedContainer):
        forward_reads = 0

        def query_items(self, **kwargs):
            rows = list(super().query_items(**kwargs))
            if "c.seq > @after" in kwargs["query"]:
                self.forward_reads += 1
                queried.set()
                if self.forward_reads > 1:
                    unrelated_read.set()
            return iter(rows)

    container = cast(ContainerProxy, Container())
    writer = CosmosTimeline(container)
    notifications = CosmosTimelineNotifications(container)
    reader = CosmosTimeline(container, notifier=notifications)
    chat = ChatService(persistence=CosmosChatPersistence(container, timeline=writer))
    root = chat.create_chat("workspace", title="Subscribed", owner_actor_id="operator")
    other = chat.create_chat("workspace", title="Other", owner_actor_id="operator")
    client = TestClient(
        create_app(
            bearer_token="token",
            chat_service=chat,
            timeline=reader,
            stream_max_poll_cycles=2,
            stream_recovery_interval_seconds=2,
        )
    )
    release_other = notifications.subscribe("workspace", other.chat_id)

    def append(chat_id: str) -> None:
        writer.append(
            workspace_id="workspace",
            chat_id=chat_id,
            kind="output",
            item_id=f"started:{chat_id}",
            thread_id="root",
            payload={
                "state": "started",
                "invocationId": chat_id,
                "messageId": f"message-{chat_id}",
                "actorId": "agent",
                "startedAt": "2026-01-01T00:00:00Z",
            },
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(
                client.get,
                f"/v1/workspaces/workspace/chats/{root.chat_id}/timeline/stream?after=0",
                headers={"Authorization": "Bearer token"},
            )
            assert queried.wait(2), response.result(timeout=3).text
            version = notifications.version("workspace", other.chat_id)
            append(other.chat_id)
            assert notifications.wait("workspace", other.chat_id, version, 1) != version
            assert not unrelated_read.wait(0.1)
            append(root.chat_id)
            result = response.result(timeout=1)
        rows = [
            json.loads(line[6:])
            for line in result.text.splitlines()
            if line.startswith("data: ")
        ]
        assert len(rows) == 1 and rows[0]["chatId"] == root.chat_id
        assert rows[0]["payload"]["state"] == "started"
    finally:
        release_other()
        notifications.close()
