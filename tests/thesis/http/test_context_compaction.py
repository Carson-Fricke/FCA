"""HTTP intake, actual session execution and durable restart across checkpoints."""

import json
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer, store_for

from thesis.http.application import create_app
from thesis_runtime.adapters.filesystem.records import FileChatPersistence
from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    Role,
    ToolCall,
)
from thesis_runtime.adapters.providers.jev import JevEvidenceRanker
from thesis_runtime.adapters.providers.openrouter import (
    OpenRouterProfile,
    OpenRouterProvider,
)
from thesis_runtime.adapters.providers.session_adapter import (
    NativeProvider,
    NativeSessionAdapter,
)
from thesis_runtime.adapters.providers.transports import HttpOpenRouterTransport
from thesis_runtime.contracts.compaction import ContextPolicy
from thesis_runtime.contracts.datum import Datum
from thesis_runtime.contracts.errors import ProviderRejectedError
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    ToolRequest,
    decode_transcript,
)
from thesis_runtime.contracts.records import (
    AgentInvocation,
    AgentInvocationState,
    ContextCheckpoint,
    MessageRole,
)
from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.service import InMemoryChatPersistence

POLICY = ContextPolicy(
    window_tokens=12_000,
    output_tokens=1_000,
    summary_tokens=1_500,
    recent_tokens=1_000,
    evidence_tokens=500,
    reserve_tokens=5_000,
)


class Provider:
    streaming = True

    def __init__(self) -> None:
        self.requests: list[tuple[ProviderMessage, ...]] = []
        self.summaries: list[tuple[ProviderMessage, ...]] = []
        self.during_summary: Callable[[], None] | None = None
        self.reject_summary = False
        self.request_compaction = False

    def stream(self, *, messages, role: Role = "primary", tools=None, **kwargs):
        del kwargs
        summary = tools is None
        (self.summaries if summary else self.requests).append(tuple(messages))
        if summary:
            if self.during_summary is not None:
                callback, self.during_summary = self.during_summary, None
                callback()
            if self.reject_summary:
                raise ProviderRejectedError("summary unavailable")
            message = ProviderMessage(
                "assistant",
                "Objective: preserve the operator's constraint. Progress: read evidence. Next: inspect job-keep.",
            )
        elif self.request_compaction:
            self.request_compaction = False
            message = ProviderMessage(
                "assistant",
                "",
                tool_calls=(
                    ToolCall("compact-once", "compact_context", '{"focus":"job-keep"}'),
                ),
            )
        else:
            message = ProviderMessage("assistant", "Done")
        yield ModelCompletion(
            message=message,
            model="test",
            provider="test",
            role=role,
            prompt_tokens=0,
            completion_tokens=0,
        )


def open_stack(
    root: Path,
    provider: NativeProvider,
    *,
    policy=POLICY,
    persistence=None,
    ranker=None,
):
    persistence = persistence or InMemoryChatPersistence()
    chat = ChatService(persistence=persistence)
    service = Conversations(
        chat=chat,
        provider=NativeSessionAdapter(provider),
        root_system_prompt="Exact configured system instruction.",
        context_policy=policy,
        evidence_ranker=ranker,
        spawn_thread=lambda run: None,
    )
    client = TestClient(create_app(bearer_token="token", conversations=service))
    return client, chat, service


def send(client: TestClient, chat_id: str, body: str):
    response = client.post(
        f"/v1/workspaces/workspace/chats/{chat_id}/messages",
        headers={"Authorization": "Bearer token"},
        json={"actor_id": "operator", "body": body},
    )
    assert response.status_code == 200, response.json()
    return response.json()


def seed(
    chat: ChatService, service: Conversations, chat_id: str, *, count=60, chars=280
):
    assignment = service.root("workspace", chat_id).send(
        "Original assignment: preserve constraint and job-keep.",
        "operator",
        schedule=False,
    )
    for index in range(count):
        chat.create_message(
            "workspace",
            chat_id,
            actor_id="model",
            body=f"evidence-{index}: " + "x" * chars,
            role=MessageRole.ASSISTANT,
        )
    chat.acknowledge_input("workspace", chat_id, "", assignment.input_position)
    return assignment


@pytest.mark.parametrize("backend", ["memory", "file", "cosmos"])
def test_automatic_compaction_preserves_late_input_and_repeated_restart(
    tmp_path: Path, backend: str
):
    provider = Provider()
    persistence = (
        FileChatPersistence(tmp_path)
        if backend == "file"
        else store_for(LimitedContainer())
        if backend == "cosmos"
        else InMemoryChatPersistence()
    )
    client, chat, service = open_stack(tmp_path, provider, persistence=persistence)
    thread = chat.create_chat(
        "workspace", title="Compaction", owner_actor_id="operator"
    )
    # Few larger messages exercise the same byte threshold without repeated
    # filesystem commits. The separate history-cap test still uses >40 rows.
    seed(chat, service, thread.chat_id, count=12, chars=2000)
    pending = send(client, thread.chat_id, "Continue with the constraint.")
    provider.during_summary = lambda: service.root("workspace", thread.chat_id).deliver(
        Datum("notification", text="late-runtime-evidence")
    )
    result = service.run_node_turn("workspace", thread.chat_id, "")
    assert result.status == "completed"
    assert len(provider.summaries) == 1
    assert provider.requests[-1][0].content == "Exact configured system instruction."
    assert any("Original assignment" in row.content for row in provider.summaries[0])
    assert any("late-runtime-evidence" in row.content for row in provider.requests[-1])
    assert any(
        "Continue with the constraint" in row.content for row in provider.requests[-1]
    )
    checkpoint = chat.read_context_checkpoint(
        "workspace", thread.chat_id, thread.chat_id
    )
    assert checkpoint is not None
    assert (
        chat.read_consumed_input("workspace", thread.chat_id, "")
        >= pending["input_position"]
    )
    durable_before = chat.list_conversation_messages(
        "workspace", thread.chat_id, thread.chat_id
    )

    # A new process reads the identical canonical view, with the new user input.
    provider = Provider()
    if backend == "file":
        persistence = FileChatPersistence(tmp_path)
    client, restarted, service = open_stack(tmp_path, provider, persistence=persistence)
    send(client, thread.chat_id, "Resume after restart")
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "completed"
    active = provider.requests[-1]
    assert active[0].content == "Exact configured system instruction."
    assert any("job-keep" in row.content for row in active)
    assert not any("evidence-0:" in row.content for row in active)
    assert len(restarted.list_messages("workspace", thread.chat_id)) > len(
        durable_before
    )
    # Fill a second window and verify an additional checkpoint survives reload.
    seed(restarted, service, thread.chat_id, count=12, chars=2000)
    send(client, thread.chat_id, "Continue a second window")
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "completed"
    second = restarted.read_context_checkpoint(
        "workspace", thread.chat_id, thread.chat_id
    )
    assert second.summary_message_id != checkpoint.summary_message_id
    provider = Provider()
    client, _, service = open_stack(tmp_path, provider, persistence=persistence)
    send(client, thread.chat_id, "Resume second checkpoint")
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "completed"
    assert any("job-keep" in row.content for row in provider.requests[-1])
    assert not any("evidence-0:" in row.content for row in provider.requests[-1])


def test_model_request_survives_slice_yield_and_complete_history_is_sent(
    tmp_path: Path,
):
    provider = Provider()
    persistence = InMemoryChatPersistence()
    client, chat, service = open_stack(
        tmp_path, provider, policy=ContextPolicy(), persistence=persistence
    )
    thread = chat.create_chat("workspace", title="Manual", owner_actor_id="operator")
    seed(chat, service, thread.chat_id)
    send(client, thread.chat_id, "Continue")
    provider.request_compaction = True
    service._max_tool_iterations = 1
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "yielded"
    assert len(provider.requests[0]) > 40
    assert any("Original assignment" in row.content for row in provider.requests[0])
    # Recreate every live object. The pending correlated tool receipt carries intent.
    provider = Provider()
    _, chat, service = open_stack(
        tmp_path, provider, policy=ContextPolicy(), persistence=persistence
    )
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "completed"
    assert len(provider.summaries) == 1
    assert (
        chat.read_context_checkpoint("workspace", thread.chat_id, thread.chat_id)
        is not None
    )
    # The tool call and exact result remain representable beside the checkpoint.
    assert any(
        row.role == "tool" and row.tool_call_id == "compact-once"
        for row in provider.requests[-1]
    )


def test_summary_uses_transcript_data_and_audited_transport_limits(tmp_path: Path):
    class Key:
        def key(self):
            return "test-key"

    requests = []

    def respond(request: httpx.Request):
        payload = json.loads(request.content)
        requests.append(payload)
        summary = payload.get("tool_choice") == "none"
        if summary:
            assert payload["max_tokens"] == POLICY.summary_tokens
            assert "tools" not in payload
            assert [row["role"] for row in payload["messages"]] == ["system", "user"]
            transcript = json.loads(payload["messages"][-1]["content"])["transcript"]
            assert any(row["message_id"] == "original-call" for row in transcript)
            assert all("tool_calls" not in row for row in payload["messages"])
        else:
            assert (
                payload["messages"][0]["content"]
                == "Exact configured system instruction."
            )
        text = (
            "Checkpoint: original assignment and work in progress."
            if summary
            else "Done"
        )
        frames = [
            {
                "id": "generation-test",
                "choices": [{"delta": {"content": text}, "finish_reason": "stop"}],
            },
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        ]
        return httpx.Response(
            200,
            text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
            + "data: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    native = OpenRouterProvider(
        OpenRouterProfile(
            profile_id="test", model="test/model", max_output_tokens=32768
        ),
        Key(),
        HttpOpenRouterTransport(
            "https://provider.example",
            client=httpx.Client(transport=httpx.MockTransport(respond)),
        ),
    )
    client, chat, service = open_stack(tmp_path, native)
    thread = chat.create_chat(
        "workspace", title="Summary protocol", owner_actor_id="operator"
    )
    call = SessionMessage(
        "original-call",
        "outgoing",
        MessageContent(
            "", "agent", requests=(ToolRequest("request", "inspect", "{}"),)
        ),
    )
    chat.create_message(
        "workspace",
        thread.chat_id,
        actor_id="model",
        body="Inspect",
        role=MessageRole.ASSISTANT,
        message_id=call.message_id,
        data={"sessionMessage": call.as_dict()},
    )
    service.root("workspace", thread.chat_id).deliver_session_message(
        SessionMessage(
            "original-result",
            "incoming",
            MessageContent(
                "Evidence",
                "tool",
                ToolCorrelation("original-call", "request", "inspect"),
            ),
        ),
        schedule=False,
    )
    seed(chat, service, thread.chat_id, count=12, chars=2000)
    send(client, thread.chat_id, "Continue")
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "completed"
    summary_audit = next(
        row
        for row in chat.persistence.list(AgentInvocation, "workspace")
        if row.provider_request and row.provider_request.get("tool_choice") == "none"
    )
    assert summary_audit.state == AgentInvocationState.SUCCEEDED
    assert summary_audit.provider_request["max_tokens"] == requests[0]["max_tokens"]
    assert (
        summary_audit.provider_request["messages"][-1]["content"]
        == requests[0]["messages"][-1]["content"]
    )


@pytest.mark.parametrize("arrival", ["during", "after", "restart"])
def test_async_calls_finish_across_real_compaction_without_retaining_requests(
    tmp_path: Path, arrival: str
):
    provider = Provider()
    persistence = (
        FileChatPersistence(tmp_path)
        if arrival == "restart"
        else InMemoryChatPersistence()
    )
    client, chat, service = open_stack(tmp_path, provider, persistence=persistence)
    thread = chat.create_chat(
        "workspace", title="Async compaction", owner_actor_id="operator"
    )
    call = SessionMessage(
        "open-request",
        "outgoing",
        MessageContent(
            "Async work",
            "agent",
            requests=tuple(
                ToolRequest(identity, "external", "{}") for identity in ("one", "two")
            ),
        ),
    )
    chat.create_message(
        "workspace",
        thread.chat_id,
        actor_id="model",
        body="Async work",
        role=MessageRole.ASSISTANT,
        message_id=call.message_id,
        data={"sessionMessage": call.as_dict()},
    )
    seed(chat, service, thread.chat_id, count=12, chars=2000)
    send(client, thread.chat_id, "Continue while those calls run")

    def result(identity):
        return SessionMessage(
            f"result-{identity}",
            "incoming",
            MessageContent(
                f"Result {identity}",
                "tool",
                ToolCorrelation(call.message_id, identity, "external"),
                {"ok": True},
            ),
        )

    def deliver(identity):
        return service.root("workspace", thread.chat_id).deliver_session_message(
            result(identity), schedule=False
        )

    if arrival == "during":
        provider.during_summary = lambda: deliver("one")
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "waiting"
    checkpoint = chat.read_context_checkpoint(
        "workspace", thread.chat_id, thread.chat_id
    )
    assert checkpoint is not None
    assert call.message_id not in checkpoint.retained_message_ids
    assert not any(row.tool_calls for row in provider.requests[-1])
    if arrival == "restart":
        provider = Provider()
        client, chat, service = open_stack(
            tmp_path, provider, persistence=FileChatPersistence(tmp_path)
        )
    if arrival != "during":
        deliver("one")
        assert (
            service.run_node_turn("workspace", thread.chat_id, "").status == "waiting"
        )
    assert any("Result one" in row.content for row in provider.requests[-1])
    assert not any(row.role == "tool" for row in provider.requests[-1])
    # Repeated delivery joins the same durable receipt, including after restart.
    first = deliver("one")
    assert deliver("one").message_id == first.message_id
    deliver("two")
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "completed"
    assert any("Result two" in row.content for row in provider.requests[-1])
    durable = chat.list_conversation_messages(
        "workspace", thread.chat_id, thread.chat_id
    )
    assert sum(row.message_id == "result-one" for row in durable) == 1
    assert chat.get_message("workspace", call.message_id).data["sessionMessage"][
        "content"
    ]["requests"] == [request.as_dict() for request in call.content.requests]


@pytest.mark.parametrize("invalid", ["tool_requests", "oversized", "empty"])
def test_invalid_summary_is_audited_without_checkpoint_ack_or_tool_dispatch(
    tmp_path: Path, invalid: str
):
    class InvalidProvider(Provider):
        def stream(self, *, messages, role="primary", tools=None, **kwargs):
            if tools is not None:
                raise AssertionError(
                    "Invalid compaction must stop before task execution"
                )
            message = ProviderMessage(
                "assistant",
                "x" * 6000 if invalid == "oversized" else "",
                tool_calls=(ToolCall("unexpected", "mutate", "{}"),)
                if invalid == "tool_requests"
                else (),
            )
            yield ModelCompletion(
                message=message,
                model="test",
                provider="test",
                role=role,
                prompt_tokens=100,
                completion_tokens=50,
            )

    persistence = InMemoryChatPersistence()
    client, chat, service = open_stack(
        tmp_path, InvalidProvider(), persistence=persistence
    )
    thread = chat.create_chat(
        "workspace", title="Invalid summary", owner_actor_id="operator"
    )
    assignment = seed(chat, service, thread.chat_id, count=12, chars=2000)
    send(client, thread.chat_id, "Still pending")
    outcome = service.run_node_turn("workspace", thread.chat_id, "")
    assert outcome.status == "interrupted"
    assert outcome.failure["kind"] == "compaction_failed"
    assert (
        chat.read_context_checkpoint("workspace", thread.chat_id, thread.chat_id)
        is None
    )
    assert (
        chat.read_consumed_input("workspace", thread.chat_id, "")
        == assignment.input_position
    )
    audit = persistence.list(AgentInvocation, "workspace")[-1]
    assert audit.state == AgentInvocationState.FAILED
    assert audit.provider_request is not None
    assert audit.failure is not None
    assert audit.provider_request["tool_choice"] == "none"
    if invalid == "empty":
        metadata = audit.failure["provider_metadata"]
        assert isinstance(metadata, Mapping)
        rejected = metadata["rejected_response"]
        assert isinstance(rejected, Mapping)
        assert rejected["content"] == ""
    else:
        assert audit.failure["validation_reason"] == invalid
        rejected = audit.failure["rejected_response"]
        assert isinstance(rejected, Mapping)
        content = rejected["content"]
        assert isinstance(content, Mapping)
        assert content["requests"] or invalid == "oversized"


@pytest.mark.parametrize("failure", ["summary", "checkpoint"])
def test_failed_compaction_retains_history_and_pending_input(
    tmp_path: Path, failure: str
):
    class Store(InMemoryChatPersistence):
        def save(self, record):
            if failure == "checkpoint" and isinstance(record, ContextCheckpoint):
                raise RuntimeError("checkpoint write failed")
            return super().save(record)

    provider = Provider()
    provider.reject_summary = failure == "summary"
    client, chat, service = open_stack(tmp_path, provider, persistence=Store())
    thread = chat.create_chat("workspace", title="Failure", owner_actor_id="operator")
    assignment = seed(chat, service, thread.chat_id)
    send(client, thread.chat_id, "Still pending")
    if failure == "checkpoint":
        with pytest.raises(RuntimeError, match="checkpoint write failed"):
            service.run_node_turn("workspace", thread.chat_id, "")
    else:
        assert (
            service.run_node_turn("workspace", thread.chat_id, "").status
            == "interrupted"
        )
    assert (
        chat.read_context_checkpoint("workspace", thread.chat_id, thread.chat_id)
        is None
    )
    assert (
        chat.read_consumed_input("workspace", thread.chat_id, "")
        == assignment.input_position
    )
    assert len(decode_transcript(chat.list_messages("workspace", thread.chat_id))) >= 62
    assert not provider.requests


@pytest.mark.parametrize("available", [True, False])
def test_optional_jev_is_audited_and_does_not_filter_summary_source(
    tmp_path: Path, available: bool
):
    provider = Provider()
    persistence = InMemoryChatPersistence()

    def respond(request: httpx.Request):
        audits = persistence.list(AgentInvocation, "workspace")
        assert any(
            row.tool_name == "context_evidence_selection"
            and row.state == AgentInvocationState.STARTED
            for row in audits
        )
        if not available:
            return httpx.Response(503)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "answers": {
                    key: {"type": "noul", "noul": 1.0} for key in payload["questions"]
                }
            },
        )

    ranker = JevEvidenceRanker(
        "test-key", client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    client, chat, service = open_stack(
        tmp_path, provider, persistence=persistence, ranker=ranker
    )
    thread = chat.create_chat("workspace", title="Jev", owner_actor_id="operator")
    call = SessionMessage(
        "history-call",
        "outgoing",
        MessageContent(
            "", "agent", requests=(ToolRequest("history-request", "inspect", "{}"),)
        ),
    )
    chat.create_message(
        "workspace",
        thread.chat_id,
        actor_id="model",
        body="Inspect",
        role=MessageRole.ASSISTANT,
        message_id=call.message_id,
        data={"sessionMessage": call.as_dict()},
    )
    service.root("workspace", thread.chat_id).deliver(
        Datum("notification", text="interleaved evidence")
    )
    service.root("workspace", thread.chat_id).deliver_session_message(
        SessionMessage(
            "history-result",
            "incoming",
            MessageContent(
                "exact-job-reference",
                "tool",
                correlation=ToolCorrelation(
                    call.message_id, "history-request", "inspect"
                ),
                result={"ok": True},
            ),
        ),
        schedule=False,
    )
    seed(chat, service, thread.chat_id)
    send(client, thread.chat_id, "Continue")
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "completed"
    assert any("evidence-0:" in row.content for row in provider.summaries[0])
    checkpoint = chat.read_context_checkpoint(
        "workspace", thread.chat_id, thread.chat_id
    )
    assert bool(checkpoint.retained_message_ids) == available
    if available:
        assert {"history-call", "history-result"}.issubset(
            checkpoint.retained_message_ids
        )
        assert any(
            row.tool_call_id == "history-request" for row in provider.requests[-1]
        )

    selector_audits = [
        row
        for row in persistence.list(AgentInvocation, "workspace")
        if row.tool_name == "context_evidence_selection"
    ]
    assert selector_audits
    assert all(
        row.state
        == (
            AgentInvocationState.SUCCEEDED if available else AgentInvocationState.FAILED
        )
        for row in selector_audits
    )
    assert all("test-key" not in str(row.as_dict()) for row in selector_audits)


def test_oversized_pending_input_is_not_silently_dropped_or_automatically_retried(
    tmp_path: Path,
):
    provider = Provider()
    client, chat, service = open_stack(tmp_path, provider)
    thread = chat.create_chat("workspace", title="Oversized", owner_actor_id="operator")
    pending = send(client, thread.chat_id, "required input " + "x" * 30_000)
    result = service.run_node_turn("workspace", thread.chat_id, "")
    assert result.status == "interrupted"
    assert result.failure["kind"] == "context_capacity"
    assert chat.read_consumed_input("workspace", thread.chat_id, "") == 0
    assert chat.get_message("workspace", pending["message_id"]).body.endswith(
        "x" * 30_000
    )
    assert not provider.requests and not provider.summaries
    assert not any(
        (row.data or {}).get("status") == "continuing"
        for row in chat.list_messages("workspace", thread.chat_id)
    )


def test_oversized_consumed_history_compacts_in_chunks_and_resumes_after_yield(
    tmp_path: Path,
):
    provider = Provider()
    persistence = InMemoryChatPersistence()
    client, chat, service = open_stack(tmp_path, provider, persistence=persistence)
    thread = chat.create_chat(
        "workspace", title="Many windows", owner_actor_id="operator"
    )
    seed(chat, service, thread.chat_id, count=240)
    send(client, thread.chat_id, "Continue the original assignment")
    service._max_tool_iterations = 2
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "yielded"
    assert len(provider.summaries) == 2 and not provider.requests
    provider_after = Provider()
    _, chat, service = open_stack(tmp_path, provider_after, persistence=persistence)
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "completed"
    assert provider_after.summaries and len(provider_after.requests) == 1
    all_sources = [
        row.content
        for batch in (*provider.summaries, *provider_after.summaries)
        for row in batch
    ]
    final_context = [row.content for row in provider_after.requests[-1]]
    for index in range(240):
        assert any(
            f"evidence-{index}:" in text for text in (*all_sources, *final_context)
        )
    assert any("Original assignment" in text for text in all_sources)
    assert any("Continue the original assignment" in text for text in final_context)


def test_usage_notice_reconciles_intervening_input_before_acknowledging(tmp_path: Path):
    class Store(InMemoryChatPersistence):
        during_notice = None

        def admit_message(self, message):
            canonical = (message.data or {}).get("sessionMessage", {})
            result = canonical.get("content", {}).get("result") or {}
            if result.get("context_usage") and self.during_notice is not None:
                callback, self.during_notice = self.during_notice, None
                callback()
            return super().admit_message(message)

    provider = Provider()
    persistence = Store()
    policy = ContextPolicy(
        window_tokens=20_000,
        output_tokens=1_000,
        summary_tokens=1_000,
        recent_tokens=1_000,
        evidence_tokens=500,
        reserve_tokens=5_000,
    )
    client, chat, service = open_stack(
        tmp_path, provider, persistence=persistence, policy=policy
    )
    thread = chat.create_chat(
        "workspace", title="Notice race", owner_actor_id="operator"
    )
    seed(chat, service, thread.chat_id, count=95)
    send(client, thread.chat_id, "Continue")
    persistence.during_notice = lambda: service.root(
        "workspace", thread.chat_id
    ).deliver(Datum("notification", text="intervening-arrival"))
    assert service.run_node_turn("workspace", thread.chat_id, "").status == "completed"
    assert not provider.summaries
    assert any("context_usage" in row.content for row in provider.requests[0])
    assert any("intervening-arrival" in row.content for row in provider.requests[0])
