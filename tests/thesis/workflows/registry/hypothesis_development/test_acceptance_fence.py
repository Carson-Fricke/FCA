"""Declaration completeness, exactly-once pipes, and acceptance races."""

from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest

from thesis.agent.runtime.control import PhaseCompletionRejected
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat.contracts import CompletionMode, SubAgentKind, SubAgentStatus
from thesis.workflows.core.phase import (
    ConversationPhase,
    DeterministicPhase,
    OneShotPhase,
    TerminalPhase,
)
from thesis.workflows.registry.hypothesis_development.phases import phase_from_value
from thesis.workflows.registry.hypothesis_development.rebuild import rebuild_state
from thesis.workflows.registry.hypothesis_development.session_services import (
    HypothesisSessionServices,
)
from thesis.workflows.registry.hypothesis_development.spec import (
    phase_declarations,
    piping,
)

from ._support import (
    WORKSPACE,
    FakePrograms,
    _discovery_envelope,
    _hypothesis_envelope,
    _kit,
    _node_record,
    _request,
)


def test_declarations_are_complete_and_executor_variants_are_explicit() -> None:
    declarations = phase_declarations()
    assert len(declarations) == 10
    assert len({item.spec.phase for item in declarations}) == len(declarations)
    for item in declarations:
        if isinstance(item.spec, TerminalPhase):
            assert item.spec.terminal and not item.edges
            assert item.orchestrator is None and item.task is None
        elif isinstance(item.spec, DeterministicPhase):
            assert item.spec.contract is not None and item.spec.validate is not None
            assert item.orchestrator is None and not item.tools
        else:
            assert isinstance(item.spec, ConversationPhase | OneShotPhase)
            assert item.spec.seed is not None and item.spec.validate is not None
            assert item.spec.required and item.spec.completion_schema is not None
            assert item.orchestrator and item.task and item.activity
            assert "search_knowledge" in item.tools
        assert all(callable(pipe) for pipe in item.edges.values())


@pytest.mark.parametrize("edge", list(piping()))
def test_every_edge_live_and_replay_use_declared_pipe_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edge: tuple[str, str]
) -> None:
    source, target = edge
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    kit.chat.set_execution_phase(WORKSPACE, request.execution_id, source)
    state = replace(
        kit.workflow._reconstruct_state(request),
        phase=phase_from_value(source),
    )
    completion = {
        "discovery": _discovery_envelope(),
        "hypothesis": _hypothesis_envelope(),
        "trials": [],
        "verdict": {"outcome": "refuted", "reason": "accepted"},
        "strategy": {
            "workspaceId": WORKSPACE,
            "kind": "strategy",
            "programId": "momentum",
            "gitCommit": "a" * 40,
            "version": "b" * 64,
        },
        "backtest": {"jobId": "run-one", "state": "complete"},
    }
    counts = []
    original = kit.workflow.spec.piping[edge]

    def pipe(current):
        counts.append(current.phase.value)
        return original(current)

    pipes = {**kit.workflow.spec.piping, edge: pipe}
    kit.workflow._spec = replace(kit.workflow.spec, piping=pipes)
    monkeypatch.setattr(kit.workflow, "_on_phase_entered", lambda *args, **kwargs: None)
    live = kit.workflow._transition(
        request, state, target, "accepted", completion=completion
    )
    assert counts == [target]
    counts.clear()
    replay = rebuild_state(
        WORKSPACE,
        request.execution_id,
        kit.execution_store.get(request.execution_id).transitions,
        initial_phase=phase_from_value(source),
        pipes=pipes,
    )
    assert counts == [target]
    assert live == replay


@pytest.mark.parametrize(
    "phase", [item.spec.phase for item in phase_declarations() if item.orchestrator]
)
def test_all_phase_agents_explicit_even_one_shot(tmp_path: Path, phase: str) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow._spawn_phase_child(request, phase, "phase seed")
    assert node is not None
    record = _node_record(kit, node)
    assert record.phase_executor is True
    assert record.completion_mode is CompletionMode.EXPLICIT_COMPLETION
    expected = (
        SubAgentKind.CONVERSATION
        if isinstance(kit.workflow.spec.phase_specs[phase], ConversationPhase)
        else SubAgentKind.ONE_SHOT
    )
    assert record.kind is expected
    assert record.phase_attempt_id == kit.workflow._phase_attempt_id(request, phase)


@pytest.mark.parametrize("when", ["before", "validator", "pipe"])
def test_steering_rejects_before_validation_and_before_commit(
    tmp_path: Path, when: str
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    calls = []

    def steer():
        kit.workflow.steer(
            WORKSPACE, request.execution_id, actor_id="operator", body="new intent"
        )

    original = kit.workflow.spec.phase_specs["discovery"]

    def validator(completion):
        calls.append("validate")
        if when == "validator":
            steer()
        return None

    pipes = dict(kit.workflow.spec.piping)
    old_pipe = pipes[("discovery", "model_creation")]

    def pipe(state):
        if when == "pipe":
            steer()
        return old_pipe(state)

    pipes[("discovery", "model_creation")] = pipe
    kit.workflow._spec = replace(
        kit.workflow.spec,
        piping=pipes,
        phases=tuple(
            replace(item, validate=validator) if item is original else item
            for item in kit.workflow.spec.phases
        ),
    )
    if when == "before":
        steer()
    with pytest.raises(PhaseCompletionRejected, match="stale"):
        kit.workflow.apply_phase_completion(
            request,
            node,
            {
                "target": "model_creation",
                "discovery": _discovery_envelope(),
                "intentEpoch": 0,
            },
        )
    assert calls == ([] if when == "before" else ["validate"])
    assert kit.execution_store.get(request.execution_id).transitions == ()


def test_acceptance_port_receives_trusted_expectations_and_typed_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Callable

    from thesis.workflows.core.execution_store import ExecutionDocument

    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    accept = kit.chat.accept_transition
    captured: list[ExecutionDocument] = []
    projected: list[str] = []
    project = kit.chat.set_execution_phase

    def project_once(workspace_id: str, execution_id: str, phase: str):
        projected.append(phase)
        return project(workspace_id, execution_id, phase)

    monkeypatch.setattr(kit.chat, "set_execution_phase", project_once)

    def inspect_acceptance(
        workspace_id: str,
        execution_id: str,
        *,
        expected_intent_epoch: int,
        expected_phase: str,
        expected_node_id: str | None,
        expected_attempt_id: str | None,
        target_phase: str,
        commit: Callable[[], ExecutionDocument],
    ) -> ExecutionDocument:
        assert (workspace_id, execution_id) == (WORKSPACE, request.execution_id)
        assert expected_intent_epoch == request.intent_epoch == 0
        assert expected_phase == "discovery"
        assert expected_node_id == node
        assert expected_attempt_id == _node_record(kit, node).phase_attempt_id
        assert target_phase == "model_creation"
        result = accept(
            workspace_id,
            execution_id,
            expected_intent_epoch=expected_intent_epoch,
            expected_phase=expected_phase,
            expected_node_id=expected_node_id,
            expected_attempt_id=expected_attempt_id,
            target_phase=target_phase,
            commit=commit,
        )
        captured.append(result)
        return result

    monkeypatch.setattr(kit.chat, "accept_transition", inspect_acceptance)
    kit.workflow.apply_phase_completion(
        request,
        node,
        {"target": "model_creation", "discovery": _discovery_envelope()},
    )
    assert captured == [kit.execution_store.get(request.execution_id)]
    assert projected == ["model_creation"]
    assert (
        kit.chat.get_execution(WORKSPACE, request.execution_id).phase
        == "model_creation"
    )


def test_commit_port_rejects_steering_after_last_engine_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    accept = kit.chat.accept_transition

    def steer_then_accept(*args, **kwargs):
        kit.workflow.steer(
            WORKSPACE, request.execution_id, actor_id="operator", body="new intent"
        )
        return accept(*args, **kwargs)

    monkeypatch.setattr(kit.chat, "accept_transition", steer_then_accept)
    with pytest.raises(PhaseCompletionRejected, match="epoch|stale"):
        kit.workflow.apply_phase_completion(
            request,
            node,
            {"target": "model_creation", "discovery": _discovery_envelope()},
        )
    assert kit.execution_store.get(request.execution_id).transitions == ()
    assert kit.chat.get_execution(WORKSPACE, request.execution_id).phase == "discovery"


@pytest.mark.parametrize("change", ["phase", "attempt", "executor", "stopped"])
def test_acceptance_port_maps_late_guard_rejection_without_committing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    accept = kit.chat.accept_transition
    store = PersistenceSubAgentStore(kit.chat.persistence)
    record = _node_record(kit, node)

    def change_then_accept(*args, **kwargs):
        if change == "phase":
            kit.chat.set_execution_phase(WORKSPACE, request.execution_id, "verdict")
        elif change == "stopped":
            kit.chat.stop_workflow(WORKSPACE, request.execution_id, actor_id="operator")
        elif change == "attempt":
            store.save_sub_agent(replace(record, phase_attempt_id="superseded"))
        else:
            store.save_sub_agent(replace(record, phase_executor=False))
        return accept(*args, **kwargs)

    monkeypatch.setattr(kit.chat, "accept_transition", change_then_accept)
    with pytest.raises(PhaseCompletionRejected, match="stale|executor|no longer"):
        kit.workflow.apply_phase_completion(
            request,
            node,
            {"target": "model_creation", "discovery": _discovery_envelope()},
        )
    assert kit.execution_store.get(request.execution_id).transitions == ()
    assert (
        kit.chat.get_execution(WORKSPACE, request.execution_id).phase
        != "model_creation"
    )


def test_commit_callback_and_projection_exclude_concurrent_steering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    attempting = Event()
    finished = Event()
    errors: list[Exception] = []
    observed: list[tuple[str | None, int]] = []
    workers: list[Thread] = []
    append = kit.execution_store.append_transition

    def steer() -> None:
        attempting.set()
        try:
            execution = kit.workflow.steer(
                WORKSPACE, request.execution_id, actor_id="operator", body="new intent"
            )
            observed.append((execution.phase, execution.intent_epoch))
        except Exception as error:  # noqa: BLE001 - surface worker faults in test
            errors.append(error)
        finally:
            finished.set()

    def append_with_steering(*args, **kwargs):
        worker = Thread(target=steer, daemon=True)
        workers.append(worker)
        worker.start()
        assert attempting.wait(2)
        assert not finished.wait(0.1), "steering escaped the acceptance lock"
        return append(*args, **kwargs)

    monkeypatch.setattr(kit.execution_store, "append_transition", append_with_steering)
    try:
        result = kit.workflow.apply_phase_completion(
            request,
            node,
            {"target": "model_creation", "discovery": _discovery_envelope()},
        )
    finally:
        for worker in workers:
            worker.join(timeout=2)
    assert finished.is_set() and not errors
    assert observed == [("model_creation", 1)]
    assert result["applied"] is True
    transitions = kit.execution_store.get(request.execution_id).transitions
    assert len(transitions) == 1 and transitions[0].intent_epoch == 0


def test_cancellation_before_acceptance_prevents_commit(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    lifecycle = kit.workflow.subagent_lifecycle
    assert lifecycle is not None
    assert lifecycle.cancel_subtree(WORKSPACE, node) == (node,)
    with pytest.raises(PhaseCompletionRejected, match="executor"):
        kit.workflow.apply_phase_completion(
            request,
            node,
            {"target": "model_creation", "discovery": _discovery_envelope()},
        )
    assert kit.execution_store.get(request.execution_id).transitions == ()
    assert _node_record(kit, node).status is SubAgentStatus.CANCELLED


@pytest.mark.parametrize("boundary", ["commit", "accepted"])
def test_source_completion_and_acceptance_exclude_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    lifecycle = kit.workflow.subagent_lifecycle
    assert lifecycle is not None
    attempting = Event()
    finished = Event()
    errors: list[Exception] = []
    cancelled: list[tuple[str, ...]] = []
    workers: list[Thread] = []
    completions: list[str] = []
    accept = kit.chat.accept_transition
    append = kit.execution_store.append_transition
    complete = lifecycle.complete
    completion = {"target": "model_creation", "discovery": _discovery_envelope()}

    def cancel() -> None:
        attempting.set()
        try:
            cancelled.append(lifecycle.cancel_subtree(WORKSPACE, node))
        except Exception as error:  # noqa: BLE001 - surface worker faults
            errors.append(error)
        finally:
            finished.set()

    def start_cancellation() -> None:
        worker = Thread(target=cancel, daemon=True)
        workers.append(worker)
        worker.start()
        assert attempting.wait(2)
        assert not finished.wait(0.1), "cancellation escaped the execution guard"

    def append_with_cancellation(*args, **kwargs):
        if boundary == "commit":
            start_cancellation()
        return append(*args, **kwargs)

    def accept_with_cancellation(*args, **kwargs):
        result = accept(*args, **kwargs)
        if boundary == "accepted":
            start_cancellation()
        assert _node_record(kit, node).status is SubAgentStatus.PENDING
        return result

    def complete_once(workspace_id, node_id, **kwargs):
        completions.append(node_id)
        assert (
            kit.chat.get_execution(WORKSPACE, request.execution_id).phase
            == "model_creation"
        )
        assert len(kit.execution_store.get(request.execution_id).transitions) == 1
        assert kwargs["result"] == completion
        return complete(workspace_id, node_id, **kwargs)

    entered = kit.workflow._on_phase_entered

    def enter_after_release(*args, **kwargs):
        assert finished.wait(2), "phase publication still holds the execution guard"
        assert _node_record(kit, node).status is SubAgentStatus.COMPLETED
        return entered(*args, **kwargs)

    monkeypatch.setattr(
        kit.execution_store, "append_transition", append_with_cancellation
    )
    monkeypatch.setattr(kit.chat, "accept_transition", accept_with_cancellation)
    monkeypatch.setattr(lifecycle, "complete", complete_once)
    monkeypatch.setattr(kit.workflow, "_on_phase_entered", enter_after_release)
    try:
        result = kit.workflow.apply_phase_completion(request, node, completion)
    finally:
        for worker in workers:
            worker.join(timeout=2)
    assert result["applied"] is True
    assert finished.is_set() and not errors
    assert cancelled == [()]
    assert completions == [node]
    assert _node_record(kit, node).result == completion
    assert kit.workflow.active_orchestrators[request.execution_id].sub_agent_id != node


@pytest.mark.parametrize("target", ["model_creation", "failed"])
def test_accepted_source_is_completed_once_before_phase_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    lifecycle = kit.workflow.subagent_lifecycle
    assert lifecycle is not None
    complete = lifecycle.complete
    emit = kit.workflow._emit_phase
    calls: list[str] = []
    completion = {"target": target, "discovery": _discovery_envelope()}

    def complete_once(workspace_id, node_id, **kwargs):
        calls.append(node_id)
        return complete(workspace_id, node_id, **kwargs)

    def emit_after_completion(*args, **kwargs):
        record = _node_record(kit, node)
        assert record.status is SubAgentStatus.COMPLETED
        assert record.result == completion
        assert kit.chat.get_execution(WORKSPACE, request.execution_id).phase == target
        return emit(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "complete", complete_once)
    monkeypatch.setattr(kit.workflow, "_emit_phase", emit_after_completion)
    kit.workflow.apply_phase_completion(request, node, completion)
    assert calls.count(node) == 1
    assert lifecycle.cancel_subtree(WORKSPACE, node) == ()
    assert _node_record(kit, node).status is SubAgentStatus.COMPLETED


def test_source_completion_save_failure_is_not_a_repairable_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    lifecycle = kit.workflow.subagent_lifecycle
    assert lifecycle is not None

    def fail_save(*args, **kwargs):
        raise OSError("source completion persistence failed")

    monkeypatch.setattr(lifecycle, "complete", fail_save)
    with pytest.raises(OSError, match="source completion persistence failed"):
        kit.workflow.apply_phase_completion(
            request,
            node,
            {"target": "model_creation", "discovery": _discovery_envelope()},
        )
    assert len(kit.execution_store.get(request.execution_id).transitions) == 1
    assert (
        kit.chat.get_execution(WORKSPACE, request.execution_id).phase
        == "model_creation"
    )
    assert kit.workflow.active_orchestrators[request.execution_id].sub_agent_id == node


@pytest.mark.parametrize("when", ["before", "activity", "after"])
def test_expensive_validation_is_fenced_by_original_epoch(
    tmp_path: Path, when: str
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    kit.workflow.apply_phase_completion(
        request, node, {"target": "model_creation", "discovery": _discovery_envelope()}
    )
    record = _node_record(
        kit, kit.workflow.active_orchestrators[request.execution_id].sub_agent_id
    )
    calls = []

    def steer():
        kit.workflow.steer(
            WORKSPACE, request.execution_id, actor_id="operator", body="new intent"
        )

    def activity(context, tool, arguments, execute):
        assert context.intent_epoch == 0 and context.phase_executor
        if when == "activity":
            steer()
        result = execute()
        calls.append(tool)
        if when == "after":
            steer()
        return result

    class Programs(FakePrograms):
        def load(self, workspace_id, kind, program_id, commit=None):
            calls.append("expensive")
            return super().load(workspace_id, kind, program_id, commit)

    service = HypothesisSessionServices(
        chat=kit.chat,
        store=PersistenceSubAgentStore(kit.chat.persistence),
        workflow=lambda: kit.workflow,
        harness_tools=None,
        programs=Programs(),
        activity=activity,
    )
    completion = {"hypothesis": _hypothesis_envelope(), "intentEpoch": 0}
    if when == "before":
        steer()
        assert "stale" in (
            service.validate_completion(WORKSPACE, record, completion) or ""
        )
        assert calls == []
    else:
        if when == "activity":
            with pytest.raises(PhaseCompletionRejected, match="stale"):
                service.validate_completion(WORKSPACE, record, completion)
            assert calls == []
        else:
            assert "stale" in (
                service.validate_completion(WORKSPACE, record, completion) or ""
            )
            assert calls == [
                "expensive",
                "expensive",
                "expensive",
                "verify_program_reference",
            ]
            with pytest.raises(PhaseCompletionRejected, match="stale"):
                service.apply_completion(WORKSPACE, record, completion)


def test_helper_cannot_complete_by_workflow_membership(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    record = replace(_node_record(kit, node), phase_executor=False)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    store.save_sub_agent(record)
    service = HypothesisSessionServices(
        chat=kit.chat,
        store=store,
        workflow=lambda: kit.workflow,
        harness_tools=None,
        activity=lambda context, tool, arguments, execute: execute(),
    )
    assert service.apply_completion(WORKSPACE, record, {"intentEpoch": 0}) is None
    assert "executor" in (
        service.validate_completion(WORKSPACE, record, {"intentEpoch": 0}) or ""
    )
    with pytest.raises(PhaseCompletionRejected, match="executor"):
        kit.workflow.apply_phase_completion(
            request,
            node,
            {"target": "model_creation", "discovery": _discovery_envelope()},
        )


def test_model_only_bindings_keep_phase_capabilities_without_harness(
    tmp_path: Path,
) -> None:
    from thesis.agent.runtime.context import ToolContext

    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    service = HypothesisSessionServices(
        chat=kit.chat,
        store=PersistenceSubAgentStore(kit.chat.persistence),
        harness_tools=None,
        activity=lambda context, tool, arguments, execute: execute(),
    )
    bindings = service.resolve(
        ToolContext(
            workspace_id=WORKSPACE,
            chat_id=request.chat_id,
            conversation_id=node,
            sub_agent_id=node,
            actor_id="actor",
            workflow_execution_id=request.execution_id,
            phase_executor=True,
        )
    )
    assert bindings.completion_schema is not None
    assert bindings.knowledge_collections == (
        "discovery",
        "data-quality",
        "trading-costs",
        "model-cards",
    )


def test_completion_epoch_is_not_ignored_when_request_is_current(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    kit.workflow.steer(
        WORKSPACE, request.execution_id, actor_id="operator", body="new intent"
    )
    request = replace(request, intent_epoch=1)
    with pytest.raises(PhaseCompletionRejected, match="stale"):
        kit.workflow.apply_phase_completion(
            request,
            node,
            {
                "target": "model_creation",
                "discovery": _discovery_envelope(),
                "intentEpoch": 0,
            },
        )
    assert kit.execution_store.get(request.execution_id).transitions == ()


@pytest.mark.parametrize("change", ["attempt", "phase_executor", "revision"])
def test_commit_rechecks_phase_owner_and_observed_store_revision(
    tmp_path: Path, change: str
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    record = _node_record(kit, node)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    original = kit.workflow.spec.piping[("discovery", "model_creation")]

    def pipe(state):
        if change == "attempt":
            store.save_sub_agent(replace(record, phase_attempt_id="new-attempt"))
        elif change == "phase_executor":
            store.save_sub_agent(replace(record, phase_executor=False))
        else:
            kit.execution_store.put_phase_result(
                request.execution_id, "discovery", {"changed": True}
            )
        return original(state)

    kit.workflow._spec = replace(
        kit.workflow.spec,
        piping={**kit.workflow.spec.piping, ("discovery", "model_creation"): pipe},
    )
    with pytest.raises(PhaseCompletionRejected, match="executor|changed"):
        kit.workflow.apply_phase_completion(
            request,
            node,
            {"target": "model_creation", "discovery": _discovery_envelope()},
        )
    assert kit.execution_store.get(request.execution_id).transitions == ()
    assert kit.chat.get_execution(WORKSPACE, request.execution_id).phase == "discovery"
    assert _node_record(kit, node).status is SubAgentStatus.PENDING
