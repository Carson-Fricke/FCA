"""Asynchronous harness evidence resumes canonical conversations without polling."""

import pytest
from tests.thesis.http.test_async_returns import ReturnProvider, tick_anchor
from tests.thesis.http.test_stateless_ownership import replica
from tests.thesis.submissions import MemorySubmissions
from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer

from thesis_runtime.adapters.providers.invocations import ProviderMessage
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    from_record,
)
from thesis_runtime.contracts.records import SubAgentKind, SubAgentStatus
from thesis_runtime.conversations.delivery import Anchor
from thesis_runtime.subagents.specs import SubAgentSpec


def terminal(job_id, state="succeeded"):
    return {
        "job_id": job_id,
        "kind": "backtest",
        "state": state,
        "metrics": {"net_pnl": "12.5"},
        "artifact_count": 1,
        "error_message": None
        if state == "succeeded"
        else 'capture.dataset: invalid name "causal/signal"; pass an unprefixed name',
    }


def pending(service, anchor, *jobs):
    service.deliver_session_message(
        anchor,
        SessionMessage(
            "admission-" + jobs[0],
            "incoming",
            MessageContent(
                "Jobs accepted", "tool", result={"ok": True, "pendingJobs": list(jobs)}
            ),
        ),
    )


def retain(submissions, thread, conversation_id, *jobs):
    context = ToolContext("workspace", thread.chat_id, conversation_id, "operator")
    for job in jobs:
        submissions.retain(
            "workspace", job, "run_backtest", {"recipient": context.as_dict()}
        )


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
def test_completed_jobs_resume_after_restart_without_polling_or_duplicate_wakes(state):
    database, submissions = LimitedContainer(), MemorySubmissions()
    provider = ReturnProvider(
        ProviderMessage(
            "assistant", "Submitted; I will use the result when it arrives."
        )
    )
    client, chat, worker, work, _ = replica(database, provider)
    thread = chat.create_chat(
        "workspace", title="Async jobs", owner_actor_id="operator"
    )
    service = client.app.state.conversations
    retain(submissions, thread, thread.chat_id, "job-one")
    pending(service, Anchor("workspace", thread.chat_id), "job-one")
    assert tick_anchor(worker, "") == 1
    assert provider.calls == 1
    assert tick_anchor(worker, "") == 0
    assert not work.active("workspace", f"turn:{thread.chat_id}:")

    resumed = ReturnProvider(ProviderMessage("assistant", "Result received"))
    client, chat, worker, _, _ = replica(database, resumed)
    client.app.state.submission_store = submissions
    url = "/v1/internal/harness/completions"
    assert client.post(url, json=terminal("job-one", state)).status_code == 401
    headers = {"Authorization": "Bearer token"}
    assert (
        client.post(url, headers=headers, json=terminal("job-one", state)).status_code
        == 204
    )
    assert tick_anchor(worker, "") == 1
    assert resumed.calls == 1
    if state == "failed":
        assert any(
            "capture.dataset" in message.content
            and "causal/signal" in message.content
            and "unprefixed" in message.content
            for message in resumed.requests[0]
        )
    assert (
        client.post(url, headers=headers, json=terminal("job-one", state)).status_code
        == 204
    )
    assert tick_anchor(worker, "") == 0
    conflicting = terminal("job-one", state)
    conflicting["metrics"] = {"net_pnl": "999"}
    assert client.post(url, headers=headers, json=conflicting).status_code == 409
    assert tick_anchor(worker, "") == 0
    rows = chat.list_conversation_messages("workspace", thread.chat_id, thread.chat_id)
    assert sum(from_record(row).content.source == "job_completion" for row in rows) == 1


@pytest.mark.parametrize("late", [False, True])
def test_one_shot_collects_partial_batch_results_and_cancelled_children_stay_closed(
    late,
):
    submissions = MemorySubmissions()
    provider = ReturnProvider(ProviderMessage("assistant", "Batch submitted"))
    client, chat, worker, _, _ = replica(LimitedContainer(), provider)
    client.app.state.submission_store = submissions
    service = client.app.state.conversations
    thread = chat.create_chat("workspace", title="Batch", owner_actor_id="operator")
    child = service.spawn(
        Anchor("workspace", thread.chat_id),
        SubAgentSpec(role="researcher", kind=SubAgentKind.ONE_SHOT),
        "Evaluate the batch",
        SubAgentKind.ONE_SHOT,
    )
    anchor = Anchor("workspace", thread.chat_id, child.conversation_id)
    retain(submissions, thread, child.conversation_id, "job-first", "job-second")
    pending(service, anchor, "job-first", "job-second")
    assert tick_anchor(worker, child.conversation_id) >= 1
    assert child.record.status is SubAgentStatus.RUNNING
    assert tick_anchor(worker, child.conversation_id) == 0
    if late:
        child.cancel()
    headers = {"Authorization": "Bearer token"}
    url = "/v1/internal/harness/completions"
    provider.responses = [ProviderMessage("assistant", "First result inspected")]
    assert (
        client.post(url, headers=headers, json=terminal("job-first")).status_code == 204
    )
    assert tick_anchor(worker, child.conversation_id) == (0 if late else 1)
    assert child.record.status is (
        SubAgentStatus.CANCELLED if late else SubAgentStatus.RUNNING
    )
    provider.responses = [ProviderMessage("assistant", "All results inspected")]
    assert (
        client.post(
            url, headers=headers, json=terminal("job-second", "failed")
        ).status_code
        == 204
    )
    assert tick_anchor(worker, child.conversation_id) == (0 if late else 1)
    assert child.record.status is (
        SubAgentStatus.CANCELLED if late else SubAgentStatus.COMPLETED
    )
    assert (
        len(
            [
                row
                for row in child.transcript()
                if from_record(row).content.source == "job_completion"
            ]
        )
        == 2
    )


def test_fast_completion_before_admission_receipt_does_not_strand_work():
    provider = ReturnProvider(ProviderMessage("assistant", "Done"))
    client, chat, worker, _, _ = replica(LimitedContainer(), provider)
    submissions = MemorySubmissions()
    client.app.state.submission_store = submissions
    service = client.app.state.conversations
    thread = chat.create_chat("workspace", title="Fast", owner_actor_id="operator")
    child = service.spawn(
        Anchor("workspace", thread.chat_id),
        SubAgentSpec(role="fast", kind=SubAgentKind.ONE_SHOT),
        "Run",
        SubAgentKind.ONE_SHOT,
    )
    retain(submissions, thread, child.conversation_id, "job-fast")
    response = client.post(
        "/v1/internal/harness/completions",
        headers={"Authorization": "Bearer token"},
        json=terminal("job-fast"),
    )
    assert response.status_code == 204
    pending(
        service, Anchor("workspace", thread.chat_id, child.conversation_id), "job-fast"
    )
    assert tick_anchor(worker, child.conversation_id) >= 1
    assert child.record.status is SubAgentStatus.COMPLETED
    assert provider.calls == 1


def test_completion_arriving_during_response_is_consumed_before_the_turn_finishes():
    provider = ReturnProvider(
        ProviderMessage("assistant", "Independent work"),
        ProviderMessage("assistant", "Inspected the new result"),
    )
    client, chat, worker, _, _ = replica(LimitedContainer(), provider)
    submissions = MemorySubmissions()
    client.app.state.submission_store = submissions
    service = client.app.state.conversations
    thread = chat.create_chat("workspace", title="Midturn", owner_actor_id="operator")
    retain(submissions, thread, thread.chat_id, "job-midturn")
    pending(service, Anchor("workspace", thread.chat_id), "job-midturn")

    def complete():
        assert (
            client.post(
                "/v1/internal/harness/completions",
                headers={"Authorization": "Bearer token"},
                json=terminal("job-midturn"),
            ).status_code
            == 204
        )

    provider.during_response = complete
    assert tick_anchor(worker, "") >= 1
    assert provider.calls == 2
    tick_anchor(worker, "")
    assert provider.calls == 2
