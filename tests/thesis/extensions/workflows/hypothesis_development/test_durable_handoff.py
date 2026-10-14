"""Durable phase dispatch survives engine replacement and rejects stale intent."""

from dataclasses import replace
from typing import cast

import pytest
from azure.cosmos import ContainerProxy
from tests.thesis.extensions.workflows.hypothesis_development._support import (
    IDEAS,
    WORKSPACE,
    _kit,
    _request,
)
from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer, store_for

from thesis.extensions.workflows.hypothesis_development import (
    build_hypothesis_development_workflow,
)
from thesis_runtime.adapters.cosmos.work import CosmosWorkStore
from thesis_runtime.subagents.store import PersistenceSubAgentStore
from thesis_runtime.workflows.workflow import PhaseCompletionRejected


def test_phase_entry_is_durable_and_can_be_entered_by_a_reconstructed_engine(tmp_path):
    container = LimitedContainer()
    queue = CosmosWorkStore(cast(ContainerProxy, container))
    kit = _kit(
        tmp_path,
        payloads=[],
        results=[],
        work_store=queue,
        persistence=store_for(container),
    )
    request = _request(kit)
    assert kit.workflow.prepare_phase(request) is None
    assert not kit.woke
    phase = next(work for work in queue.ready() if work.kind == "workflow")
    rebuilt = build_hypothesis_development_workflow(
        chat=kit.chat,
        runner=kit.runner,
        hypothesis_store=kit.store,
        conversations=kit.conversations,
        execution_store=kit.execution_store,
        work_store=queue,
    )
    rebuilt.enter_scheduled_phase(request, phase.payload)
    nodes = PersistenceSubAgentStore(kit.chat.persistence).list_sub_agents(WORKSPACE)
    executors = [node for node in nodes if node.phase_executor]
    assert len(executors) == 1
    transcript = kit.chat.list_conversation_messages(
        WORKSPACE, request.chat_id, executors[0].sub_agent_id
    )
    assert any(IDEAS in row.body for row in transcript)
    assert any(work.kind == "conversation" for work in queue.ready())
    rebuilt.prepare_phase(request)
    assert len([work for work in queue.ready() if work.kind == "workflow"]) == 1


def test_queued_phase_cannot_adopt_a_new_intent_epoch(tmp_path):
    container = LimitedContainer()
    queue = CosmosWorkStore(cast(ContainerProxy, container))
    kit = _kit(
        tmp_path,
        payloads=[],
        results=[],
        work_store=queue,
        persistence=store_for(container),
    )
    request = _request(kit)
    kit.workflow.prepare_phase(request)
    phase = next(work for work in queue.ready() if work.kind == "workflow")
    execution = kit.chat.steer_workflow(
        WORKSPACE, request.execution_id, actor_id="operator", body="Change direction"
    )
    with pytest.raises(PhaseCompletionRejected, match="intent epoch"):
        kit.workflow.enter_scheduled_phase(
            replace(request, intent_epoch=execution.intent_epoch), phase.payload
        )
    nodes = PersistenceSubAgentStore(kit.chat.persistence).list_sub_agents(WORKSPACE)
    assert not [node for node in nodes if node.phase_executor]
