"""Research-mandate plumbing, completion enforcement, trials, and seed sections.

The mandate is derived from the execution's persisted launch request. Tests use
literal persisted partitions where the boundary must be exact, and the real
launch path where persistence is the behavior under test.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat import ChatService
from thesis.chat.contracts import SubAgentRecord, WorkflowDefinition
from thesis.experiments import MandateError, derive_mandate, rfc3339_ns
from thesis.workflows.registry.hypothesis_development.contracts.discovery import (
    DiscoveryDraft,
)
from thesis.workflows.registry.hypothesis_development.session_services import (
    HypothesisSessionServices,
)
from thesis.workflows.research_models import ProgramSnapshots

from ._support import (
    WORKSPACE,
    FakePrograms,
    _discovery_envelope,
    _hypothesis_envelope,
    _kit,
    _request,
)

CREATED_AT = "2026-09-04T12:00:00Z"
PARTITION: dict[str, object] = {
    "historyDays": 1825,
    "forwardDays": 90,
    "purgeDays": 1,
    "embargoDays": 0,
    "provenance": "launch-v2",
}
LAUNCH_REQUEST: dict[str, object] = {
    "createdAt": CREATED_AT,
    "partition": PARTITION,
    "dependencies": {
        "historyDays": 1825,
        "forwardDays": 90,
        "informationHorizonDays": 1,
        "positionHorizonDays": 1,
        "liquidationDays": 0,
    },
}


class WindowPrograms(FakePrograms):
    """A committed program whose configuration carries the given window."""

    def __init__(self, start: str, end: str) -> None:
        super().__init__()
        self._window = (start, end)

    def load(self, workspace_id, kind, program_id, commit=None):
        snapshot = dict(super().load(workspace_id, kind, program_id, commit))
        start, end = self._window
        snapshot["configuration"] = {
            **snapshot["configuration"],
            "start": start,
            "end": end,
        }
        return snapshot


def _crossing() -> WindowPrograms:
    """A committed study whose configuration window crosses the forward boundary."""
    return WindowPrograms("2026-08-01T00:00:00Z", "2026-10-01T00:00:00Z")


def _store_of(launch_request: Mapping[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        get=lambda execution_id: SimpleNamespace(launch_request=dict(launch_request))
    )


def _save_record(
    chat: ChatService,
    chat_id: str,
    execution_id: str,
    *,
    executor: bool = True,
    phase: str = "discovery",
) -> tuple[PersistenceSubAgentStore, SubAgentRecord]:
    store = PersistenceSubAgentStore(chat.persistence)
    record = SubAgentRecord(
        workspace_id=WORKSPACE,
        chat_id=chat_id,
        sub_agent_id="phase-node",
        role="research",
        created_by="actor",
        created_at=datetime.now(UTC),
        phase=phase,
        workflow_execution_id=execution_id,
        phase_executor=executor,
        phase_attempt_id="attempt",
    )
    store.save_sub_agent(record)
    return store, record


def _simple_workflow(execution_store: object, phase: str) -> SimpleNamespace:
    return SimpleNamespace(
        spec=SimpleNamespace(
            completion_schemas={},
            knowledge_scope="both",
            phase_knowledge={phase: ()},
        ),
        execution_store=execution_store,
        _reconstruct_state=lambda request: SimpleNamespace(
            phase=SimpleNamespace(value=phase),
            discovery=DiscoveryDraft.from_dict(_discovery_envelope()),
        ),
        _workflow_node_id=lambda request: "phase-node",
        _phase_attempt_id=lambda request, value: "attempt",
    )


def _resolver(
    execution_store: object,
    *,
    phase: str = "discovery",
) -> tuple[
    HypothesisSessionServices, ToolContext, PersistenceSubAgentStore, SubAgentRecord
]:
    """Bind one phase agent's services to a workflow with the given store."""
    chat = ChatService()
    thread = chat.create_chat(WORKSPACE, title="Mandate", owner_actor_id="operator")
    execution = chat.launch_workflow(
        WORKSPACE,
        thread.chat_id,
        actor_id="operator",
        body="Develop an idea",
        workflow=WorkflowDefinition(
            workflow_id="hypothesis-development",
            version="1.0.0",
            title="Development",
            phases=(phase,),
        ),
    )
    store, record = _save_record(
        chat, thread.chat_id, execution.execution_id, phase=phase
    )
    workflow = _simple_workflow(execution_store, phase)
    service = HypothesisSessionServices(
        chat=chat,
        store=store,
        workflow=cast(Any, lambda: workflow),
        harness_tools=None,
        programs=FakePrograms(),
        activity=lambda context, tool, arguments, execute: execute(),
    )
    context = ToolContext(
        workspace_id=WORKSPACE,
        chat_id=record.chat_id,
        conversation_id=record.sub_agent_id,
        sub_agent_id=record.sub_agent_id,
        actor_id="actor",
        workflow_execution_id=execution.execution_id,
    )
    return service, context, store, record


def test_launch_persists_the_partition_in_the_execution_document(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    document = kit.execution_store.get(request.execution_id)
    assert document.launch_request["partition"] == PARTITION
    mandate = derive_mandate(WORKSPACE, request.execution_id, document.launch_request)
    assert mandate.development.purged_ns == 86_400_000_000_000
    assert mandate.forward.embargoed_ns == 0


def test_bindings_carry_the_launch_policy_for_program_phases(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    document = kit.execution_store.get(request.execution_id)
    mandate = derive_mandate(WORKSPACE, request.execution_id, document.launch_request)
    store, record = _save_record(kit.chat, request.chat_id, request.execution_id)
    service = HypothesisSessionServices(
        chat=kit.chat,
        store=store,
        workflow=lambda: kit.workflow,
        harness_tools=None,
        programs=FakePrograms(),
        activity=lambda context, tool, arguments, execute: execute(),
    )
    context = ToolContext(
        workspace_id=WORKSPACE,
        chat_id=record.chat_id,
        conversation_id=record.sub_agent_id,
        sub_agent_id=record.sub_agent_id,
        actor_id="actor",
        workflow_execution_id=request.execution_id,
    )
    revision = kit.execution_store.get(request.execution_id).revision
    bindings = service.resolve(context)
    assert bindings.data_window == mandate.default_window
    assert bindings.data_window is not None
    assert bindings.data_window.label == "development"
    assert kit.execution_store.get(request.execution_id).revision == revision
    store.save_sub_agent(replace(record, phase_executor=False))
    helper_bindings = service.resolve(context)
    assert helper_bindings.programs is None
    assert helper_bindings.data_window is None


def test_bindings_carry_the_forward_override_policy_for_forward_validation(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    document = kit.execution_store.get(request.execution_id)
    mandate = derive_mandate(WORKSPACE, request.execution_id, document.launch_request)
    store, record = _save_record(
        kit.chat,
        request.chat_id,
        request.execution_id,
        phase="forward_validation",
    )
    service = HypothesisSessionServices(
        chat=kit.chat,
        store=store,
        workflow=lambda: kit.workflow,
        harness_tools=None,
        programs=FakePrograms(),
        activity=lambda context, tool, arguments, execute: execute(),
    )
    context = ToolContext(
        workspace_id=WORKSPACE,
        chat_id=record.chat_id,
        conversation_id=record.sub_agent_id,
        sub_agent_id=record.sub_agent_id,
        actor_id="actor",
        workflow_execution_id=request.execution_id,
    )
    bindings = service.resolve(context)
    assert bindings.programs is not None
    assert bindings.data_window == mandate.forward_window
    assert bindings.data_window is not None
    assert bindings.data_window.label == "forward"
    assert bindings.data_window.override is True

    store.save_sub_agent(replace(record, phase="variation"))
    development_bindings = service.resolve(context)
    assert development_bindings.data_window == mandate.default_window
    assert development_bindings.data_window is not None
    assert development_bindings.data_window.override is False


def test_bindings_follow_the_persisted_partition() -> None:
    service, context, _, _ = _resolver(
        _store_of({**LAUNCH_REQUEST, "partition": {**PARTITION, "purgeDays": 3}})
    )
    window = service.resolve(context).data_window
    assert window is not None
    assert window.label == "development"
    assert window.start_ns == rfc3339_ns("2021-09-05T12:00:00Z")
    assert window.end_ns == rfc3339_ns("2026-06-03T12:00:00Z")


@pytest.mark.parametrize(
    "launch_request",
    [
        {},
        {"createdAt": None, "partition": PARTITION},
        {"createdAt": "not-a-date", "partition": PARTITION},
        {"createdAt": CREATED_AT},
        {"createdAt": CREATED_AT, "partition": {"historyDays": 1825}},
    ],
    ids=["empty", "null-created-at", "unparseable", "no-partition", "partial"],
)
def test_bindings_fail_closed_without_a_usable_launch_partition(
    launch_request: Mapping[str, object],
) -> None:
    service, context, _, _ = _resolver(_store_of(launch_request))
    with pytest.raises(MandateError):
        service.resolve(context)


def test_bindings_fail_closed_when_the_execution_document_is_missing() -> None:
    def missing(execution_id: str) -> object:
        raise KeyError(execution_id)

    for execution_store in (None, SimpleNamespace(get=missing)):
        service, context, _, _ = _resolver(execution_store)
        with pytest.raises(MandateError):
            service.resolve(context)


def test_helper_agents_do_not_need_a_mandate() -> None:
    service, context, store, record = _resolver(None)
    store.save_sub_agent(replace(record, phase_executor=False))
    bindings = service.resolve(context)
    assert bindings.programs is None
    assert bindings.data_window is None


def _completion_for(phase: str) -> dict[str, object]:
    if phase == "model_creation":
        model = dict(cast(dict, _hypothesis_envelope()["model"]))
        model["workspaceId"] = "workspace"
        return {
            "hypothesis": {**_hypothesis_envelope(), "model": model},
            "intentEpoch": 0,
        }
    return {
        "strategy": {
            "workspaceId": "workspace",
            "kind": "strategy",
            "programId": "momentum",
            "gitCommit": "a" * 40,
            "version": "b" * 64,
        },
        "intentEpoch": 0,
    }


def _completion_service(
    phase: str, programs: ProgramSnapshots
) -> tuple[HypothesisSessionServices, SubAgentRecord, PersistenceSubAgentStore]:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Mandate", owner_actor_id="operator")
    execution = chat.launch_workflow(
        "workspace",
        thread.chat_id,
        actor_id="operator",
        body="Develop an idea",
        workflow=WorkflowDefinition(
            workflow_id="hypothesis-development",
            version="1.0.0",
            title="Development",
            phases=(phase,),
        ),
    )
    record = SubAgentRecord(
        workspace_id="workspace",
        chat_id=thread.chat_id,
        sub_agent_id="author",
        role="coding",
        created_by="actor",
        created_at=datetime.now(UTC),
        phase=phase,
        workflow_execution_id=execution.execution_id,
        phase_executor=True,
        phase_attempt_id="attempt",
        parent_sub_agent_id="phase-node",
    )
    store = PersistenceSubAgentStore(chat.persistence)
    store.save_sub_agent(record)
    workflow = _simple_workflow(_store_of(LAUNCH_REQUEST), phase)
    service = HypothesisSessionServices(
        chat=chat,
        store=store,
        workflow=cast(Any, lambda: workflow),
        harness_tools=None,
        programs=programs,
        activity=lambda context, tool, arguments, execute: execute(),
    )
    return service, record, store


@pytest.mark.parametrize("phase", ["model_creation", "strategy_design"])
def test_completion_rejects_a_forward_crossing_configuration_window(
    phase: str,
) -> None:
    service, record, _ = _completion_service(phase, _crossing())
    failure = service.validate_completion("workspace", record, _completion_for(phase))
    assert failure is not None
    assert "2026-06-06T12:00:00Z" in failure
    assert "2026-06-05T12:00:00Z" in failure


@pytest.mark.parametrize("phase", ["model_creation", "strategy_design"])
def test_completion_rejects_a_configuration_starting_before_the_development_window(
    phase: str,
) -> None:
    programs = WindowPrograms("2021-09-04T23:00:00Z", "2024-03-01T00:00:00Z")
    service, record, _ = _completion_service(phase, programs)
    failure = service.validate_completion("workspace", record, _completion_for(phase))
    assert failure is not None
    assert "2021-09-05T12:00:00Z" in failure


@pytest.mark.parametrize("phase", ["model_creation", "strategy_design"])
def test_completion_rejects_a_configuration_ending_inside_the_purge_gap(
    phase: str,
) -> None:
    programs = WindowPrograms("2024-01-01T00:00:00Z", "2026-06-05T18:00:00Z")
    service, record, _ = _completion_service(phase, programs)
    failure = service.validate_completion("workspace", record, _completion_for(phase))
    assert failure is not None
    assert "2026-06-05T12:00:00Z" in failure


@pytest.mark.parametrize("phase", ["model_creation", "strategy_design"])
def test_completion_accepts_a_configuration_ending_at_the_purged_development_end(
    phase: str,
) -> None:
    programs = WindowPrograms("2024-01-01T00:00:00Z", "2026-06-05T12:00:00Z")
    service, record, _ = _completion_service(phase, programs)
    assert (
        service.validate_completion("workspace", record, _completion_for(phase)) is None
    )
