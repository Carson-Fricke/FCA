"""Workflow boundaries contain technical recovery without waking ancestor agents."""

import pytest
from tests.thesis.http.test_async_returns import ReturnProvider, tick_anchor
from tests.thesis.http.test_stateless_ownership import replica
from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer

from thesis_runtime.adapters.providers.invocations import ProviderMessage
from thesis_runtime.contracts.errors import ProviderRejectedError
from thesis_runtime.contracts.records import (
    WORKFLOW_NODE_ROLE,
    SubAgentKind,
    SubAgentStatus,
)
from thesis_runtime.conversations.delivery import Anchor
from thesis_runtime.subagents.specs import SubAgentSpec


@pytest.mark.parametrize("nested", [False, True], ids=["root", "agent"])
@pytest.mark.parametrize("workflow", [False, True], ids=["helper", "workflow-phase"])
def test_child_recovery_respects_its_explicit_return_boundary(nested, workflow):
    container = LimitedContainer()
    provider = ReturnProvider(
        ProviderMessage("assistant", "Waiting for delegated work")
    )
    client, chat, worker, work, _ = replica(container, provider)
    service = client.app.state.conversations
    thread = chat.create_chat("workspace", title="Recovery", owner_actor_id="operator")
    supervisor = Anchor("workspace", thread.chat_id)
    if nested:
        parent = service.spawn(
            supervisor,
            SubAgentSpec(role="research-lead"),
            "Supervise delegated work",
            SubAgentKind.CONVERSATION,
        )
        supervisor = Anchor("workspace", thread.chat_id, parent.conversation_id)
        assert tick_anchor(worker, supervisor.schedule_id) == 1
    return_anchor = supervisor
    if workflow:
        # A workflow record owns phase conversations but never runs a model.
        record = service.lifecycle.spawn(
            "workspace",
            thread.chat_id,
            spec=SubAgentSpec(role=WORKFLOW_NODE_ROLE),
            actor_id="thesis-workflow",
            parent_sub_agent_id=supervisor.sub_agent_id,
            returns_to_sub_agent_id=supervisor.sub_agent_id,
        )
        return_anchor = Anchor("workspace", thread.chat_id, record.sub_agent_id)
    child = service.spawn(
        return_anchor,
        SubAgentSpec(role="investigator"),
        "Investigate the delegated question",
        SubAgentKind.CONVERSATION,
    )

    def interrupt():
        raise ProviderRejectedError("Provider rejected the request", status=400)

    provider.during_response = interrupt
    calls_before = provider.calls
    assert tick_anchor(worker, child.conversation_id) == 1
    assert provider.calls == calls_before + 1
    assert child.record.status is SubAgentStatus.RUNNING
    assert work.active("workspace", f"turn:{thread.chat_id}:{child.conversation_id}")
    assert work.active(
        "workspace", f"turn:{thread.chat_id}:{supervisor.schedule_id}"
    ) is (not workflow)
    if nested:
        assert not work.active("workspace", f"turn:{thread.chat_id}:")

    base = f"/v1/workspaces/workspace/chats/{thread.chat_id}"
    headers = {"Authorization": "Bearer token"}
    response = client.get(base + "/messages", headers=headers)
    assert response.status_code == 200, response.json()
    messages = response.json()["items"]
    notices = [
        row
        for row in messages
        if (row["data"]["sessionMessage"]["content"].get("result") or {}).get(
            "childRecovery"
        )
    ]
    assert len(notices) == int(not workflow)
    if notices:
        assert notices[0]["data"].get("subAgentId") == supervisor.sub_agent_id
    continuations = [
        row
        for row in messages
        if (row["data"]["sessionMessage"]["content"].get("result") or {}).get(
            "continuation"
        )
    ]
    assert len(continuations) == 1
    assert continuations[0]["data"]["subAgentId"] == child.conversation_id

    # A replacement worker resumes the same child and retained failure evidence.
    provider.responses = [ProviderMessage("assistant", "Recovered")]
    client, chat, worker, _, _ = replica(container, provider)
    response = client.post(
        base + f"/sub_agents/{child.conversation_id}/messages",
        headers=headers,
        json={"body": "Continue the investigation"},
    )
    assert response.status_code == 200, response.json()
    assert tick_anchor(worker, child.conversation_id) >= 1
    assert provider.calls == calls_before + 2
    assert (
        chat.list_conversation_messages(
            "workspace", thread.chat_id, child.conversation_id
        )[-1].body
        == "Recovered"
    )
