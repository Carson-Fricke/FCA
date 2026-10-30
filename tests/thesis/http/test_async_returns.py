"""Transcript-derived pending calls keep turns open without model wait tools."""

import json
from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from tests.thesis.http.test_context_compaction import seed
from tests.thesis.http.test_stateless_ownership import Provider, replica
from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer

from thesis.http.application import create_app
from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    ToolCall,
)
from thesis_runtime.adapters.providers.session_adapter import NativeSessionAdapter
from thesis_runtime.contracts.datum import Datum
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    ToolRequest,
    from_record,
)
from thesis_runtime.contracts.records import (
    ContextCheckpoint,
    MessageRole,
    SubAgentKind,
    SubAgentStatus,
)
from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.delivery import Anchor
from thesis_runtime.subagents.specs import SubAgentSpec


class ReturnProvider(Provider):
    def __init__(self, *responses: ProviderMessage) -> None:
        super().__init__()
        self.responses = list(responses)
        self.requests: list[tuple[ProviderMessage, ...]] = []
        self.during_response: Callable[[], object] | None = None

    def stream(self, *, message_id=None, **kwargs):
        del message_id
        self.calls += 1
        self.requests.append(tuple(kwargs["messages"]))
        if self.during_response is not None:
            callback, self.during_response = self.during_response, None
            callback()
        assert self.responses, "Waiting work invoked the model without incoming input"
        yield ModelCompletion(
            message=self.responses.pop(0),
            model="test",
            provider="test",
            role=kwargs["role"],
            prompt_tokens=0,
            completion_tokens=0,
        )


def spawn_calls(count=1):
    return ProviderMessage(
        "assistant",
        "",
        tool_calls=tuple(
            ToolCall(
                f"spawn-{i}",
                "spawn_sub_agent",
                json.dumps({"role": "helper", "task": f"Work {i}"}),
            )
            for i in range(count)
        ),
    )


def start(client, chat):
    thread = chat.create_chat(
        "workspace", title="Async returns", owner_actor_id="operator"
    )
    response = client.post(
        f"/v1/workspaces/workspace/chats/{thread.chat_id}/messages",
        headers={"Authorization": "Bearer token"},
        json={"actor_id": "operator", "body": "Delegate and collect the results"},
    )
    assert response.status_code == 200, response.json()
    return thread.chat_id


def tick_anchor(worker, anchor_id):
    return worker.tick(
        tuple(
            row
            for row in worker.store.ready()
            if worker.store.get("workspace", row.key).payload["anchor_id"] == anchor_id
        )
    )


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_normal_stop_parks_until_child_return_across_worker_replacement(outcome):
    container = LimitedContainer()
    provider = ReturnProvider(spawn_calls(), ProviderMessage("assistant", "Delegated"))
    client, chat, worker, work, _ = replica(container, provider)
    chat_id = start(client, chat)
    assert tick_anchor(worker, "") == 1
    assert provider.calls == 2
    service = client.app.state.conversations
    child_id = service.children("workspace", chat_id)[0].sub_agent_id
    assert not work.active("workspace", f"turn:{chat_id}:")
    assert tick_anchor(worker, "") == 0

    resumed = ReturnProvider(ProviderMessage("assistant", "Collected"))
    client, chat, worker, _, _ = replica(container, resumed)
    service = client.app.state.conversations
    child = service.get("workspace", child_id)
    if outcome == "completed":
        child.complete({"summary": "Child result"})
    elif outcome == "failed":
        child.fail("Child failure")
    else:
        response = client.post(
            f"/v1/workspaces/workspace/chats/{chat_id}/sub_agents/{child_id}/cancel",
            headers={"Authorization": "Bearer token"},
        )
        assert response.status_code == 200, response.json()
    assert tick_anchor(worker, "") == 1
    assert resumed.calls == 1
    assert any(outcome in row.content for row in resumed.requests[0])
    assert tick_anchor(worker, "") == 0


def test_one_shot_waits_for_every_child_and_can_process_partial_returns():
    provider = ReturnProvider(spawn_calls(2), ProviderMessage("assistant", "Delegated"))
    client, chat, worker, _, _ = replica(LimitedContainer(), provider)
    service = client.app.state.conversations
    thread = chat.create_chat("workspace", title="Parent", owner_actor_id="operator")
    parent = service.spawn(
        Anchor("workspace", thread.chat_id),
        SubAgentSpec(
            role="parent",
            kind=SubAgentKind.ONE_SHOT,
            allowed_tools=("spawn_sub_agent",),
        ),
        "Delegate the work",
        SubAgentKind.ONE_SHOT,
        actor_id="operator",
    )
    assert tick_anchor(worker, parent.conversation_id) == 1
    assert parent.record.status is SubAgentStatus.RUNNING
    # Compact away the handle messages. Liveness still comes from the full
    # durable transcript, rather than whichever messages fit the model context.
    through = chat.list_conversation_messages(
        "workspace", thread.chat_id, parent.conversation_id
    )[-1]
    summary = chat.create_message(
        "workspace",
        thread.chat_id,
        actor_id="model",
        body="One child remains.",
        role=MessageRole.ASSISTANT,
        data={"subAgentId": parent.conversation_id},
    )
    chat.save_context_checkpoint(
        ContextCheckpoint(
            workspace_id="workspace",
            chat_id=thread.chat_id,
            conversation_id=parent.conversation_id,
            root_conversation_id=thread.chat_id,
            parent_conversation_id=thread.chat_id,
            created_at=summary.created_at,
            created_by="model",
            checkpoint_id=parent.conversation_id,
            summary_message_id=summary.message_id,
            through_message_id=through.message_id,
        )
    )
    parent.deliver(Datum("notification", text="Check progress"))
    provider.responses = [ProviderMessage("assistant", "Still waiting")]
    assert tick_anchor(worker, parent.conversation_id) == 1
    assert parent.record.status is SubAgentStatus.RUNNING
    children = service.children("workspace", parent.conversation_id)
    assert len(children) == 2
    first = service.get("workspace", children[0].sub_agent_id)
    first.complete({"summary": "First result"})
    provider.responses = [ProviderMessage("assistant", "First result processed")]
    assert tick_anchor(worker, parent.conversation_id) == 1
    assert parent.record.status is SubAgentStatus.RUNNING
    second = service.get("workspace", children[1].sub_agent_id)
    second.complete({"summary": "Second result"})
    provider.responses = [ProviderMessage("assistant", "All results processed")]
    assert tick_anchor(worker, parent.conversation_id) == 1
    assert parent.record.status is SubAgentStatus.COMPLETED
    assert parent.result == {"summary": "All results processed"}


def seed_unreturned(chat, chat_id):
    message = SessionMessage(
        "pending-request",
        "outgoing",
        MessageContent(
            "", "agent", requests=(ToolRequest("pending-call", "external_tool", "{}"),)
        ),
    )
    chat.create_message(
        "workspace",
        chat_id,
        actor_id="model",
        body="External request",
        role=MessageRole.ASSISTANT,
        message_id=message.message_id,
        data={"sessionMessage": message.as_dict()},
    )


def return_call(service, chat_id, *, tool_name="external_tool"):
    return service.root("workspace", chat_id).deliver_session_message(
        SessionMessage(
            "result-" + tool_name,
            "incoming",
            MessageContent(
                "External result",
                "tool",
                correlation=ToolCorrelation(
                    "pending-request",
                    "pending-call",
                    tool_name,
                ),
                result={"ok": True},
            ),
        )
    )


def test_exact_request_result_join_is_reconstructed_from_durable_messages():
    provider = ReturnProvider(ProviderMessage("assistant", "Waiting for result"))
    container = LimitedContainer()
    client, chat, worker, _, _ = replica(container, provider)
    chat_id = start(client, chat)
    seed_unreturned(chat, chat_id)
    assert tick_anchor(worker, "") == 1
    assert provider.calls == 1
    assert tick_anchor(worker, "") == 0
    resumed = ReturnProvider(
        ProviderMessage("assistant", "Unmatched evidence processed")
    )
    client, chat, worker, _, _ = replica(container, resumed)
    service = client.app.state.conversations
    return_call(service, chat_id, tool_name="wrong_tool")
    assert tick_anchor(worker, "") == 1
    assert resumed.calls == 1
    assert tick_anchor(worker, "") == 0
    resumed.responses = [ProviderMessage("assistant", "Final result processed")]
    delivered = return_call(service, chat_id)
    assert tick_anchor(worker, "") == 1
    assert any(row.tool_call_id == "pending-call" for row in resumed.requests[-1])
    assert (
        chat.read_consumed_input("workspace", chat_id, "") == delivered.input_position
    )


def test_result_automatically_wakes_a_replaced_worker_after_real_compaction(
    monkeypatch,
):
    monkeypatch.setenv("THESIS_CONTEXT_WINDOW_TOKENS", "50000")
    monkeypatch.setenv("THESIS_MAX_OUTPUT_TOKENS", "1000")

    class CompactingProvider(ReturnProvider):
        def stream(self, *, message_id=None, **kwargs):
            if kwargs.get("tools") is None:
                yield ModelCompletion(
                    message=ProviderMessage(
                        "assistant", "Checkpoint: external work remains open."
                    ),
                    model="test",
                    provider="test",
                    role=kwargs["role"],
                    prompt_tokens=100,
                    completion_tokens=20,
                )
            else:
                yield from super().stream(message_id=message_id, **kwargs)

    container = LimitedContainer()
    provider = CompactingProvider(
        ProviderMessage("assistant", "Waiting for external work")
    )
    client, chat, worker, _, _ = replica(container, provider)
    service = client.app.state.conversations
    chat_id = start(client, chat)
    seed_unreturned(chat, chat_id)
    seed(chat, service, chat_id, count=20, chars=5000)
    service.root("workspace", chat_id).send("Continue with pending work", "operator")
    assert tick_anchor(worker, "") >= 1
    assert provider.calls == 1
    checkpoint = chat.read_context_checkpoint("workspace", chat_id, chat_id)
    assert checkpoint is not None
    assert "pending-request" not in checkpoint.retained_message_ids
    assert tick_anchor(worker, "") == 0

    resumed = CompactingProvider(
        ProviderMessage("assistant", "External result processed")
    )
    client, chat, worker, _, _ = replica(container, resumed)
    service = client.app.state.conversations
    returned = return_call(service, chat_id)
    assert tick_anchor(worker, "") == 1
    assert resumed.calls == 1
    assert any("External result" in row.content for row in resumed.requests[0])
    assert not any(row.role == "tool" or row.tool_calls for row in resumed.requests[0])
    # A context-usage notice may be consumed in the same model snapshot.
    assert chat.read_consumed_input("workspace", chat_id, "") >= returned.input_position
    assert tick_anchor(worker, "") == 0


@pytest.mark.parametrize("correlated", [False, True])
def test_arrival_during_normal_stop_is_processed_without_a_lost_local_wake(correlated):
    provider = ReturnProvider(
        ProviderMessage("assistant", "Stopping"),
        ProviderMessage("assistant", "Received"),
    )
    runs: list[Callable[[], None]] = []
    chat = ChatService()
    service = Conversations(
        chat=chat, provider=NativeSessionAdapter(provider), spawn_thread=runs.append
    )
    client = TestClient(create_app(bearer_token="token", conversations=service))
    chat_id = start(client, chat)
    if correlated:
        seed_unreturned(chat, chat_id)
        provider.during_response = lambda: return_call(service, chat_id)
    else:
        provider.during_response = lambda: service.root("workspace", chat_id).deliver(
            Datum("notification", text="Ready")
        )
    runs.pop()()
    assert provider.calls == 2
    assert runs == []
    assert not service.in_flight("workspace", chat_id, "")


def test_rejected_spawn_is_a_returned_failure_not_outstanding_work():
    provider = ReturnProvider(
        ProviderMessage(
            "assistant", "", tool_calls=(ToolCall("bad", "spawn_sub_agent", "{}"),)
        ),
        ProviderMessage("assistant", "Rejected"),
    )
    client, chat, worker, _, _ = replica(LimitedContainer(), provider)
    chat_id = start(client, chat)
    assert tick_anchor(worker, "") == 1
    assert provider.calls == 2
    assert tick_anchor(worker, "") == 0
    messages = [from_record(row) for row in chat.list_messages("workspace", chat_id)]
    failure = next(
        cast(Any, row.content.result)["failure"]
        for row in messages
        if row.content.correlation
    )
    assert failure["kind"] == "invalid_arguments"
