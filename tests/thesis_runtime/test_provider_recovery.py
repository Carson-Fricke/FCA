"""Automatic continuation through real conversations and the OpenRouter HTTP port."""

import json
from collections.abc import Mapping
from typing import cast

import httpx
import pytest
from azure.cosmos import ContainerProxy
from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer
from tests.thesis_runtime.adapters.providers.test_openrouter_transport import _Key
from tests.thesis_runtime.test_arrival_reconciliation import _service

from thesis_runtime.adapters.cosmos.ownership import CosmosOwnershipStore
from thesis_runtime.adapters.providers.invocations import messages_payload
from thesis_runtime.adapters.providers.openrouter import (
    OpenRouterProfile,
    OpenRouterProvider,
)
from thesis_runtime.adapters.providers.session_adapter import (
    NativeSessionAdapter,
    project_native_messages,
)
from thesis_runtime.adapters.providers.transports import HttpOpenRouterTransport
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    decode_transcript,
)
from thesis_runtime.contracts.records import (
    AgentInvocation,
    AgentInvocationState,
    SubAgentStatus,
)
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.storage.ownership import OwnershipLost, own


def event(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


SUCCESS = (
    event({"choices": [{"delta": {"content": "Recovered"}, "finish_reason": "stop"}]})
    + b"data: [DONE]\n\n"
)
PARTIAL = event(
    {
        "choices": [
            {
                "delta": {
                    "content": "Partial answer",
                    "reasoning": "Received reasoning",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "never-dispatch",
                            "type": "function",
                            "function": {
                                "name": "check",
                                "arguments": '{"incomplete":',
                            },
                        }
                    ],
                }
            }
        ]
    }
)


class DroppedStream(httpx.SyncByteStream):
    def __iter__(self):
        yield PARTIAL
        raise httpx.ReadError("socket closed")


class Exchange:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        response = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.SyncByteStream):
            return httpx.Response(200, stream=response)
        if isinstance(response, tuple):
            status, headers = response
            return httpx.Response(status, headers=headers)
        return httpx.Response(200, content=response)


@pytest.fixture
def backoff(monkeypatch):
    # The external clock is the only replaced collaborator. No wall-clock waits.
    sleeps = []
    monkeypatch.setattr("thesis_runtime.sessions.recovery.sleep", sleeps.append)
    return sleeps


def openrouter(exchange):
    client = httpx.Client(transport=httpx.MockTransport(exchange))
    provider = OpenRouterProvider(
        OpenRouterProfile("test", "test/model"),
        _Key(),
        HttpOpenRouterTransport("https://openrouter.ai/api/v1", client=client),
    )
    return provider


def conversation(exchange, *, phase=True, cosmos=True):
    service, anchor = _service(openrouter(exchange), phase=phase, cosmos=cosmos)
    if not phase:
        service.root(anchor.workspace_id, anchor.chat_id).send(
            "Initial", "operator", schedule=False
        )
    return service, anchor


@pytest.mark.parametrize("phase", [False, True], ids=["root", "phase"])
@pytest.mark.parametrize(
    "failure",
    [
        DroppedStream(),
        PARTIAL,
        event({"error": {"code": 502, "message": "private upstream details"}}),
    ],
)
def test_transient_failure_recovers_with_partial_evidence_and_new_audit(
    phase, failure, backoff
):
    exchange = Exchange(failure, SUCCESS)
    service, anchor = conversation(exchange, phase=phase)
    result = service.run_node_turn(
        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
    )
    assert result.status == "completed" and result.content == "Recovered"
    assert len(exchange.requests) == 2
    assert sum(backoff) == 1
    canonical = decode_transcript(service.transcript(anchor))
    recovery = [
        m for m in canonical if (m.content.result or {}).get("providerRecovery")
    ]
    assert len(recovery) == 1 and recovery[0].content.source == "runtime"
    partials = [m for m in canonical if (m.content.result or {}).get("interrupted")]
    if isinstance(failure, httpx.SyncByteStream) or failure == PARTIAL:
        assert len(partials) == 1
        assert partials[0].content.text == "Partial answer"
        assert partials[0].content.result is not None
        assert partials[0].content.result["reasoning"] == "Received reasoning"
        assert not partials[0].content.requests
    else:
        assert partials == []
    projected = project_native_messages(canonical[:-1])
    assert exchange.requests[-1]["messages"][-len(projected) :] == list(
        messages_payload(projected)
    )
    audits = service.chat.persistence.list_conversation(
        AgentInvocation,
        anchor.workspace_id,
        anchor.chat_id,
        conversation_id=anchor.sub_agent_id or anchor.chat_id,
    )
    assert [a.state for a in audits] == [
        AgentInvocationState.FAILED,
        AgentInvocationState.SUCCEEDED,
    ]
    assert audits[0].invocation_id != audits[1].invocation_id
    assert audits[0].failure is not None and audits[0].failure["retry_safe"] is False
    assert all(a.intent_epoch == 0 for a in audits)
    assert (
        service.chat.read_consumed_input(
            anchor.workspace_id, anchor.chat_id, anchor.schedule_id
        )
        >= recovery[0].position
    )


@pytest.mark.parametrize("reasoning", ["Retained reasoning", " \n"])
def test_whitespace_answer_before_disconnect_never_creates_unpersisted_context(
    reasoning, backoff
):
    partial = event(
        {"choices": [{"delta": {"content": " \n", "reasoning": reasoning}}]}
    )
    exchange = Exchange(partial, SUCCESS)
    service, anchor = conversation(exchange)
    result = service.run_node_turn(
        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
    )
    assert result.status == "completed"
    canonical = decode_transcript(service.transcript(anchor))
    partials = [m for m in canonical if (m.content.result or {}).get("interrupted")]
    assert len(partials) == int(bool(reasoning.strip()))
    if partials:
        assert partials[0].content.text.strip()
        assert partials[0].content.result is not None
        assert partials[0].content.result["reasoning"] == reasoning
    ids = {m.message_id for m in canonical}
    audits = service.chat.persistence.list_conversation(
        AgentInvocation,
        anchor.workspace_id,
        anchor.chat_id,
        conversation_id=anchor.schedule_id,
    )
    assert all(
        ref["direction"] == "instruction" or ref["message_id"] in ids
        for ref in audits[-1].request_messages
    )


def test_restart_retains_partial_evidence_and_resumes_same_agent_without_replaying(
    backoff,
):
    exchange = Exchange(PARTIAL)
    service, anchor = conversation(exchange)
    result = service.run_node_turn(
        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
    )
    assert result.status == "interrupted" and len(exchange.requests) == 4
    before = decode_transcript(service.transcript(anchor))
    exchange.responses = [SUCCESS]
    restarted = Conversations(
        chat=service.chat,
        provider=NativeSessionAdapter(openrouter(exchange)),
        spawn_thread=lambda _: None,
    )
    assert len(exchange.requests) == 4
    restarted.lifecycle.resume(anchor.workspace_id, anchor.schedule_id)
    restarted.deliver_session_message(
        anchor,
        SessionMessage("resume", "incoming", MessageContent("Continue", "user")),
        schedule=False,
    )
    result = restarted.run_node_turn(
        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
    )
    assert result.status == "completed" and len(exchange.requests) == 5
    after = decode_transcript(restarted.transcript(anchor))
    assert {m.message_id for m in before} < {m.message_id for m in after}
    assert len({m.message_id for m in after}) == len(after)
    assert any(
        "Received reasoning" in m["content"] for m in exchange.requests[-1]["messages"]
    )
    assert not any(m.get("tool_calls") for m in exchange.requests[-1]["messages"])


def test_persistent_disconnect_exhausts_budget_without_acknowledging_input(backoff):
    exchange = Exchange(httpx.ReadError("offline"))
    service, anchor = conversation(exchange)
    result = service.run_node_turn(
        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
    )
    assert result.status == "interrupted" and len(exchange.requests) == 4
    assert sum(backoff) == 7
    assert (
        service.chat.read_consumed_input(
            anchor.workspace_id, anchor.chat_id, anchor.schedule_id
        )
        == 0
    )
    canonical = decode_transcript(service.transcript(anchor))
    attempts = [
        cast(Mapping[str, object], (m.content.result or {})["providerRecovery"])[
            "attempt"
        ]
        for m in canonical
        if "providerRecovery" in (m.content.result or {})
    ]
    assert attempts == [1, 2, 3]
    assert not any(m.direction == "outgoing" for m in canonical)
    node = service.store.get_sub_agent(anchor.workspace_id, anchor.schedule_id)
    assert node is not None and node.status is SubAgentStatus.NEEDS_INPUT


@pytest.mark.parametrize(
    "failure",
    [
        (400, {}),
        (401, {}),
        (402, {}),
        (403, {}),
        b"data: broken JSON\n\n",
        event({"error": {"code": 401}}),
        httpx.LocalProtocolError("invalid headers"),
    ],
)
def test_permanent_rejection_does_not_retry(failure, backoff):
    exchange = Exchange(failure, SUCCESS)
    service, anchor = conversation(exchange)
    result = service.run_node_turn(
        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
    )
    assert result.status == "interrupted"
    assert len(exchange.requests) == 1 and backoff == []
    assert result.failure is not None and result.failure["recoverable"] is False


@pytest.mark.parametrize(
    ("delay", "calls", "wait"),
    [
        ("3", 2, 3),
        ("120", 1, 0),
        ("invalid", 2, 1),
        ("Fri, 01 Jan 2100 00:00:00 GMT", 1, 0),
    ],
)
def test_retry_after_is_honored_or_long_wait_is_returned_to_operator(
    delay, calls, wait, backoff
):
    exchange = Exchange((429, {"Retry-After": delay}), SUCCESS)
    service, anchor = conversation(exchange)
    result = service.run_node_turn(
        anchor.workspace_id, anchor.chat_id, anchor.schedule_id
    )
    assert len(exchange.requests) == calls and sum(backoff) == wait
    assert result.status == ("completed" if calls == 2 else "interrupted")


@pytest.mark.parametrize("action", ["steer", "cancel", "ownership-loss"])
def test_backoff_yields_to_operator_and_ownership(action, monkeypatch):
    exchange = Exchange(httpx.ReadError("offline"), SUCCESS)
    service, anchor = conversation(exchange, cosmos=False)
    ownership = CosmosOwnershipStore(cast(ContainerProxy, LimitedContainer()))
    claim = ownership.acquire(anchor.workspace_id, "conversation", 60)
    assert claim is not None
    fired = False

    def sleep(_):
        nonlocal fired
        if fired:
            return
        fired = True
        if action == "steer":
            service.deliver_session_message(
                anchor,
                SessionMessage(
                    "steering", "incoming", MessageContent("New direction", "user")
                ),
                schedule=False,
            )
        elif action == "cancel":
            service.lifecycle.cancel_subtree(anchor.workspace_id, anchor.schedule_id)
        else:
            ownership.release(claim)

    monkeypatch.setattr("thesis_runtime.sessions.recovery.sleep", sleep)

    def run():
        with own(ownership, claim):
            return service.run_node_turn(
                anchor.workspace_id, anchor.chat_id, anchor.schedule_id
            )

    if action == "ownership-loss":
        with pytest.raises(OwnershipLost):
            run()
    else:
        result = run()
        assert result.status == ("completed" if action == "steer" else "cancelled")
    assert len(exchange.requests) == (2 if action == "steer" else 1)
    if action == "steer":
        assert any(
            "New direction" in m["content"] for m in exchange.requests[-1]["messages"]
        )
