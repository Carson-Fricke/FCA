"""Discovery-owned window persistence, scoped access and completion enforcement."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from tests.thesis.extensions.workflows.hypothesis_development._support import (
    _phase_attempt,
)

from thesis.experiments import rfc3339_ns
from thesis.extensions.workflows.hypothesis_development.session_services import (
    HypothesisSessionServices,
)
from thesis.workflows.research_models import ProgramSnapshots
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.records import SubAgentRecord
from thesis_runtime.conversations import ChatService
from thesis_runtime.subagents.store import PersistenceSubAgentStore
from thesis_runtime.workflows.execution_store import StoreTransition

from ._support import (
    NOW,
    WORKSPACE,
    FakePrograms,
    _discovery_envelope,
    _hypothesis_envelope,
    _kit,
    _node_record,
    _request,
    _research_envelope,
)


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
        conversation_id="phase-node",
        root_conversation_id=chat_id,
        parent_conversation_id=chat_id,
    )

    store.save_sub_agent(record)

    return store, record


def test_idea_only_launch_then_discovery_freezes_windows_before_empirical_work(
    tmp_path: Path,
) -> None:

    from thesis.experiments.mandate import resolve_research_mandate
    from thesis.extensions.tools.registry import build_session_registry

    kit = _kit(tmp_path, payloads=[], results=[])

    thread = kit.chat.create_chat(
        WORKSPACE, title="Simple launch", owner_actor_id="operator"
    )

    idea = "MSFT momentum. January–July for development; August–now for forward OOS."

    trigger = kit.chat.send_message(
        WORKSPACE, thread.chat_id, actor_id="operator", body=idea
    )

    request = kit.workflow.launch(
        WORKSPACE,
        thread.chat_id,
        actor_id="operator",
        inputs={"idea": idea},
        trigger_message_id=trigger.message_id,
    )

    document = kit.execution_store.get(request.execution_id)

    assert document.launch_request["ideas"] == idea

    assert "partition" not in document.launch_request

    assert (
        resolve_research_mandate(
            kit.execution_store,
            workspace_id=WORKSPACE,
            execution_id=request.execution_id,
        )
        is None
    )

    node = kit.workflow.prepare_phase(request)

    assert node is not None

    record = _node_record(kit, node)

    store = PersistenceSubAgentStore(kit.chat.persistence)

    service = HypothesisSessionServices(
        chat=kit.chat,
        store=store,
        extension=kit.extension,
        workflow=lambda: kit.workflow,
        harness_tools=None,
        programs=FakePrograms(),
        activity=lambda context, tool, arguments, execute: execute(),
    )

    context = ToolContext(
        workspace_id=WORKSPACE,
        chat_id=thread.chat_id,
        conversation_id=node,
        sub_agent_id=node,
        actor_id="operator",
        workflow_execution_id=request.execution_id,
        phase_attempt_id=record.phase_attempt_id,
        phase_executor=True,
        allowed_tools=record.allowed_tools,
    )

    bindings = service.resolve(context)

    assert bindings.programs is None and bindings.data_window is None

    registry = build_session_registry(context=context, workflow_tools=bindings.tools)

    windows = {
        "development": {"start": "2026-01-01T00:00:00Z", "end": "2026-08-01T00:00:00Z"},
        "forward": {"start": "2026-08-01T00:00:00Z", "end": "2026-09-04T12:00:00Z"},
    }

    declared = registry.dispatch("set_research_window", windows, context=context)

    assert declared.ok

    frozen = kit.execution_store.get(request.execution_id)

    assert registry.dispatch("set_research_window", windows, context=context).ok

    assert kit.execution_store.get(request.execution_id).revision == frozen.revision

    changed = {
        **windows,
        "forward": {**windows["forward"], "start": "2026-08-02T00:00:00Z"},
    }

    assert not registry.dispatch("set_research_window", changed, context=context).ok

    bindings = service.resolve(context)

    assert bindings.programs is not None and bindings.data_window is not None

    assert bindings.data_window.permits(
        rfc3339_ns("2026-01-01T00:00:00Z"), rfc3339_ns("2026-08-01T00:00:00Z")
    )

    assert not bindings.data_window.permits(
        rfc3339_ns("2026-07-31T00:00:00Z"), rfc3339_ns("2026-08-02T00:00:00Z")
    )

    # A rebuilt service reads the same declaration without moving boundaries.

    rebuilt = HypothesisSessionServices(
        chat=kit.chat,
        store=store,
        extension=kit.extension,
        workflow=lambda: kit.workflow,
        harness_tools=None,
        programs=FakePrograms(),
        activity=lambda context, tool, arguments, execute: execute(),
    )

    assert rebuilt.resolve(context).data_window == bindings.data_window

    store.save_sub_agent(replace(record, phase_executor=False))

    helper = rebuilt.resolve(context)

    assert helper.programs is None and not helper.tools

    assert not registry.dispatch("set_research_window", windows, context=context).ok


def test_forward_phase_receives_only_its_frozen_holdout_policy(tmp_path: Path) -> None:

    kit = _kit(tmp_path, payloads=[], results=[])

    request = _request(kit)

    store, record = _save_record(
        kit.chat, request.chat_id, request.execution_id, phase="forward_validation"
    )

    service = HypothesisSessionServices(
        chat=kit.chat,
        store=store,
        extension=kit.extension,
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

    assert bindings.data_window is not None and bindings.data_window.override

    assert bindings.data_window.start_ns == rfc3339_ns("2026-06-06T12:00:00Z")

    assert bindings.data_window.end_ns == rfc3339_ns("2026-09-04T12:00:00Z")

    assert not bindings.tools


def _completion_for(phase: str) -> dict[str, object]:

    if phase == "model_creation":
        model = dict(cast(dict, _hypothesis_envelope()["model"]))

        model["workspaceId"] = WORKSPACE

        return {
            "hypothesis": {**_hypothesis_envelope(), "model": model},
            "intentEpoch": 0,
        }

    return {
        "strategy": {
            "workspaceId": WORKSPACE,
            "kind": "strategy",
            "programId": "momentum",
            "gitCommit": "a" * 40,
            "version": "b" * 64,
        },
        "intentEpoch": 0,
    }


def _completion_service(tmp_path: Path, phase: str, programs: ProgramSnapshots):

    kit = _kit(tmp_path, payloads=[], results=[], programs=programs)

    request = _request(kit)

    discovery = {"target": "model_creation", "discovery": _discovery_envelope()}

    node = kit.workflow.prepare_phase(request)

    assert node is not None

    kit.workflow.apply_phase_completion(
        request,
        node,
        discovery,
        attempt_id=_phase_attempt(kit.workflow, request.workspace_id, node),
    )

    if phase == "strategy_design":
        for target, completion in (
            ("falsification_trials", {"hypothesis": _hypothesis_envelope()}),
            ("verdict", {"trials": [], "research": _research_envelope()}),
            (
                "strategy_design",
                {"verdict": {"outcome": "supported", "reason": "accepted"}},
            ),
        ):
            document = kit.execution_store.get(request.execution_id)

            transition = StoreTransition(
                len(document.transitions) + 1,
                document.transitions[-1].to_phase,
                target,
                NOW,
                0,
                "accepted",
                completion,
                attempt_id=kit.workflow.graph_state(request)
                .positions[document.transitions[-1].to_phase]
                .attempt_id,
            )

            kit.execution_store.append_transition(
                request.execution_id, transition, expected_revision=document.revision
            )

        kit.chat.set_execution_phases(WORKSPACE, request.execution_id, (phase,))

    node = kit.workflow.prepare_phase(request)

    assert node is not None

    record = _node_record(kit, node)

    store = PersistenceSubAgentStore(kit.chat.persistence)

    service = HypothesisSessionServices(
        chat=kit.chat,
        store=store,
        workflow=lambda: kit.workflow,
        extension=kit.extension,
        harness_tools=None,
        programs=programs,
        activity=lambda context, tool, arguments, execute: execute(),
    )

    return service, record, store


@pytest.mark.parametrize("phase", ["model_creation", "strategy_design"])
def test_completion_rejects_a_forward_crossing_configuration_window(
    tmp_path: Path,
    phase: str,
) -> None:

    service, record, _ = _completion_service(tmp_path, phase, _crossing())

    failure = service.validate_completion(WORKSPACE, record, _completion_for(phase))

    assert failure is not None

    assert "2026-06-06T12:00:00Z" in failure

    assert "2026-06-05T12:00:00Z" in failure


@pytest.mark.parametrize("phase", ["model_creation", "strategy_design"])
def test_completion_rejects_a_configuration_starting_before_the_development_window(
    tmp_path: Path,
    phase: str,
) -> None:

    programs = WindowPrograms("2021-09-04T23:00:00Z", "2024-03-01T00:00:00Z")

    service, record, _ = _completion_service(tmp_path, phase, programs)

    failure = service.validate_completion(WORKSPACE, record, _completion_for(phase))

    assert failure is not None

    assert "2021-09-05T12:00:00Z" in failure


@pytest.mark.parametrize("phase", ["model_creation", "strategy_design"])
def test_completion_rejects_a_configuration_ending_inside_the_declared_gap(
    tmp_path: Path,
    phase: str,
) -> None:

    programs = WindowPrograms("2024-01-01T00:00:00Z", "2026-06-05T18:00:00Z")

    service, record, _ = _completion_service(tmp_path, phase, programs)

    failure = service.validate_completion(WORKSPACE, record, _completion_for(phase))

    assert failure is not None

    assert "2026-06-05T12:00:00Z" in failure


@pytest.mark.parametrize("phase", ["model_creation", "strategy_design"])
def test_completion_accepts_a_configuration_ending_at_the_development_end(
    tmp_path: Path,
    phase: str,
) -> None:

    programs = WindowPrograms("2024-01-01T00:00:00Z", "2026-06-05T12:00:00Z")

    service, record, _ = _completion_service(tmp_path, phase, programs)

    assert (
        service.validate_completion(WORKSPACE, record, _completion_for(phase)) is None
    )
