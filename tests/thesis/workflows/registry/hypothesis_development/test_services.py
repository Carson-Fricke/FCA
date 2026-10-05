"""Workflow-owned session policies and completion delegation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.session import ToolExecution
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat import ChatService
from thesis.chat.contracts import SubAgentRecord
from thesis.workflows.registry.hypothesis_development.session_services import (
    HypothesisSessionServices,
)
from thesis.workflows.runtime import WorkflowRuntime

from ._support import FakePrograms, _hypothesis_envelope


def test_validation_uses_injected_activity_and_propagates_writer_failure() -> None:
    from thesis.chat.contracts import WorkflowDefinition

    chat = ChatService()
    thread = chat.create_chat("workspace", title="Services", owner_actor_id="operator")
    execution = chat.launch_workflow(
        "workspace",
        thread.chat_id,
        actor_id="operator",
        body="Develop an idea",
        workflow=WorkflowDefinition(
            workflow_id="hypothesis-development",
            version="1.0.0",
            title="Development",
            phases=("model_creation",),
        ),
    )
    record = SubAgentRecord(
        workspace_id="workspace",
        chat_id=thread.chat_id,
        sub_agent_id="author",
        role="coding",
        created_by="actor",
        created_at=datetime.now(UTC),
        phase="model_creation",
        workflow_execution_id=execution.execution_id,
        phase_executor=True,
        phase_attempt_id="attempt",
    )
    contexts: list[ToolContext] = []

    def activity(
        context: ToolContext,
        tool: str,
        arguments: Mapping[str, object],
        execute: Callable[[], ToolExecution],
    ) -> ToolExecution:
        contexts.append(context)
        assert tool == "verify_program_reference"
        assert "reference" in arguments
        assert execute().ok
        raise OSError("writer unavailable")

    store = PersistenceSubAgentStore(chat.persistence)
    store.save_sub_agent(record)
    service = HypothesisSessionServices(
        chat=chat,
        store=store,
        harness_tools=None,
        programs=FakePrograms(),
        activity=activity,
    )
    with pytest.raises(OSError, match="writer unavailable"):
        service.validate_completion(
            "workspace",
            record,
            {
                "hypothesis": {
                    **_hypothesis_envelope(),
                    "model": {
                        **cast(dict, _hypothesis_envelope()["model"]),
                        "workspaceId": "workspace",
                    },
                },
                "intentEpoch": 0,
            },
        )
    assert len(contexts) == 1
    assert contexts[0].conversation_id == record.sub_agent_id
    assert contexts[0].workflow_execution_id == record.workflow_execution_id
    assert contexts[0].actor_id == record.created_by


@pytest.mark.parametrize(
    "phase", ["strategy_design", "strategy_backtesting", "optimization"]
)
def test_session_policy_is_workflow_owned_and_authoring_is_anchor_scoped(
    phase: str,
) -> None:
    chat = ChatService()
    store = PersistenceSubAgentStore(chat.persistence)
    record = SubAgentRecord(
        workspace_id="workspace",
        chat_id="chat",
        sub_agent_id="designer",
        role="coding",
        created_by="actor",
        created_at=datetime.now(UTC),
        phase=phase,
        workflow_execution_id="execution",
        phase_executor=True,
        phase_attempt_id="attempt",
    )
    store.save_sub_agent(record)
    schema = {"type": "object"}
    workflow = SimpleNamespace(
        spec=SimpleNamespace(
            completion_schemas={phase: schema},
            knowledge_scope="workspace",
            phase_knowledge={phase: ("strategy-authoring",)},
        )
    )
    artifacts = cast("Any", object())
    service = HypothesisSessionServices(
        chat=chat,
        store=store,
        workflow=cast("Any", lambda: workflow),
        harness_tools=None,
        activity=lambda context, tool, arguments, execute: execute(),
        programs=artifacts,
    )
    context = ToolContext(
        workspace_id="workspace",
        chat_id="chat",
        conversation_id="designer",
        sub_agent_id="designer",
        actor_id="actor",
        workflow_execution_id="execution",
    )
    bindings = service.resolve(context)
    assert bindings.completion_schema is schema
    assert bindings.knowledge_scope == "workspace"
    assert bindings.knowledge_collections == ("strategy-authoring",)
    assert bindings.programs is artifacts
    assert bindings.strategy_authoring is None
    assert (
        service.resolve(
            ToolContext(
                workspace_id="workspace",
                chat_id="chat",
                conversation_id="chat",
                actor_id="actor",
            )
        ).programs
        is None
    )
    denied = service.resolve(
        ToolContext(
            workspace_id="workspace",
            chat_id="other",
            conversation_id="designer",
            sub_agent_id="designer",
            actor_id="actor",
            workflow_execution_id="execution",
        )
    )
    assert denied.programs is None
    assert denied.completion_schema is None
    assert denied.knowledge_collections == ()
    store.save_sub_agent(replace(record, phase_executor=False))
    assert service.resolve(context).programs is None


def test_runtime_caches_default_workflow_per_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from thesis.agent.skills import SkillRegistry
    from thesis.workflows.runtime import build_model_router

    router = build_model_router(
        role_policies={}, model_profiles={}, providers={}, skills=SkillRegistry()
    )
    runtime = WorkflowRuntime(
        router=router, runner=cast("Any", SimpleNamespace(run=lambda request: None))
    )
    builds: list[ChatService] = []

    def build(*, chat: ChatService, **kwargs: object) -> Any:
        builds.append(chat)
        return object()

    monkeypatch.setattr(runtime, "_build_hypothesis_development", build)
    first, second = ChatService(), ChatService()
    assert runtime.build_hypothesis_development(
        chat=first
    ) is runtime.build_hypothesis_development(chat=first)
    runtime.build_hypothesis_development(chat=second)
    runtime.build_hypothesis_development(chat=first, experiment_id="explicit")
    assert builds == [first, second, first]


def test_helper_knowledge_tracks_persisted_execution_not_parent_attempt() -> None:
    from thesis.chat.contracts import WorkflowExecution

    chat = ChatService()
    execution = WorkflowExecution(
        workspace_id="workspace",
        execution_id="execution",
        chat_id="chat",
        trigger_message_id="trigger",
        workflow_id="hypothesis-development",
        workflow_version="2.0.0",
        requested_by="operator",
        created_by="operator",
        created_at=datetime.now(UTC),
        phase="discovery",
    )
    chat.persistence.save(execution)
    store = PersistenceSubAgentStore(chat.persistence)
    helper = SubAgentRecord(
        workspace_id="workspace",
        chat_id="chat",
        sub_agent_id="helper",
        role="research",
        created_by="operator",
        created_at=datetime.now(UTC),
        workflow_execution_id="execution",
        phase="strategy_design",
        phase_attempt_id="execution:strategy_design:0",
        phase_executor=False,
    )
    store.save_sub_agent(helper)
    service = HypothesisSessionServices(
        chat=chat,
        store=store,
        harness_tools=None,
        programs=FakePrograms(),
        activity=lambda context, tool, arguments, execute: execute(),
    )
    context = ToolContext(
        workspace_id="workspace",
        chat_id="chat",
        conversation_id="helper",
        sub_agent_id="helper",
        workflow_execution_id="execution",
        actor_id="operator",
    )
    bindings = service.resolve(context)
    assert bindings.knowledge_collections == (
        "discovery",
        "data-quality",
        "trading-costs",
        "model-cards",
    )
    assert bindings.completion_schema is None and bindings.programs is None
    chat.persistence.save(replace(execution, phase="model_creation"))
    bindings = service.resolve(context)
    assert bindings.knowledge_collections == (
        "model-cards",
        "inference",
        "validation",
        "trading-costs",
    )
    assert bindings.completion_schema is None and bindings.programs is None
    assert (
        service.resolve(replace(context, chat_id="foreign")).knowledge_collections == ()
    )


def test_completion_and_failure_delegate_to_the_workflow() -> None:
    from thesis.chat.contracts import WorkflowDefinition

    chat = ChatService()
    thread = chat.create_chat("workspace", title="Services", owner_actor_id="operator")
    execution = chat.launch_workflow(
        "workspace",
        thread.chat_id,
        actor_id="operator",
        body="Develop an idea",
        workflow=WorkflowDefinition(
            workflow_id="hypothesis-development",
            version="1.0.0",
            title="Development",
            phases=("model_creation",),
        ),
    )
    record = SubAgentRecord(
        workspace_id="workspace",
        chat_id=thread.chat_id,
        sub_agent_id="author",
        role="coding",
        created_by="actor",
        created_at=datetime.now(UTC),
        phase="model_creation",
        workflow_execution_id=execution.execution_id,
        phase_executor=True,
        phase_attempt_id="attempt",
        parent_sub_agent_id="owner",
    )
    calls: list[object] = []
    result = {"applied": True}

    def apply(
        request: Any, node_id: str, completion: Mapping[str, object]
    ) -> Mapping[str, object]:
        calls.append((request.execution_id, node_id, completion))
        return result

    def fail(request: Any, *, reason: str) -> None:
        calls.append((request.execution_id, reason))

    workflow = SimpleNamespace(
        apply_phase_completion=apply,
        fail_execution=fail,
        _reconstruct_state=lambda request: SimpleNamespace(
            phase=SimpleNamespace(value="model_creation")
        ),
        _workflow_node_id=lambda request: "owner",
        _phase_attempt_id=lambda request, phase: "attempt",
    )
    store = PersistenceSubAgentStore(chat.persistence)
    store.save_sub_agent(record)
    service = HypothesisSessionServices(
        chat=chat,
        store=store,
        workflow=cast("Any", lambda: workflow),
        harness_tools=None,
        activity=lambda context, tool, arguments, execute: execute(),
    )
    completion = {"summary": "Accepted", "intentEpoch": 0}
    assert service.apply_completion("workspace", record, completion) is result
    service.fail(record, ValueError("deterministic failure"))
    assert calls == [
        (execution.execution_id, record.sub_agent_id, completion),
        (execution.execution_id, "deterministic failure"),
    ]
