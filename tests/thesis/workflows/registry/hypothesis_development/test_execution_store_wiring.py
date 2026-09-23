"""Execution-store wiring (Part B, Phase 2).

The engine commits every transition — carrying its completion contract — to
the execution store before projecting any event, records the launch identity
and request at launch, and rebuilds the typed state from the store alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thesis.research.models import ResearchModelRef
from thesis.workflows.backtests import BacktestRunResult
from thesis.workflows.core.execution_store import (
    ExecutionIdentity,
    ExecutionStoreConflict,
    InMemoryExecutionStore,
)
from thesis.workflows.core.workflow import Workflow, WorkflowPorts
from thesis.workflows.registry.hypothesis_development import (
    HypothesisDevelopmentPhase,
)
from thesis.workflows.research_models import ResearchModelRunResult

from ._support import IDEAS, WORKSPACE, _hypothesis_envelope, _kit, _request


class _CompletedStudyRunner:
    """A research-model port returning one completed study run."""

    def run(self, request: object) -> ResearchModelRunResult:
        del request
        return ResearchModelRunResult(
            model_reference=ResearchModelRef(
                workspace_id=WORKSPACE,
                model_id="momentum-model",
                content_sha256="a" * 64,
            ),
            indicator_references=(),
            backtest=BacktestRunResult(
                job_id="study-1",
                state="completed",
                metrics={"sharpe": 1.0, "sample_valid": 1.0},
            ),
            workspace_artifact="workspace-model:momentum-model",
            runtime_configuration={},
            fitted_reference=None,
        )


def _complete_creation(kit, request: object) -> None:
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    kit.workflow.apply_phase_completion(
        request,
        node_id,
        {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
    )


def test_launch_records_identity_and_the_launch_request(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)

    document = kit.execution_store.get(request.execution_id)

    identity = document.identity
    assert identity.execution_id == request.execution_id
    assert identity.workspace_id == WORKSPACE
    assert identity.chat_id == request.chat_id
    assert identity.workflow_id == "hypothesis-development"
    assert identity.workflow_version == "1.0.0"
    assert identity.requested_by == "owner-1"
    # The launch request is durable: a rebuild never reaches into the chat.
    assert document.launch_request["ideas"] == IDEAS
    assert document.transitions == ()


def test_transitions_commit_to_the_store_with_their_completions(
    tmp_path: Path,
) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    _complete_creation(kit, request)

    document = kit.execution_store.get(request.execution_id)
    moves = [(item.from_phase, item.to_phase) for item in document.transitions]
    assert moves == [
        ("hypothesis_creation", "falsification_trials"),
        ("falsification_trials", "verdict"),
    ]
    creation, trials = document.transitions
    assert creation.completion["hypothesis"]["claim"] == _hypothesis_envelope()["claim"]
    assert creation.reason == "phase completion accepted for 'hypothesis_creation'"
    assert trials.completion["trials"]
    assert trials.completion["trials"][0]["runState"] == "completed"
    assert [item.sequence for item in document.transitions] == [1, 2]


def test_the_store_commit_lands_before_the_phase_projection(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)

    log: list[tuple[str, str]] = []

    class _RecordingStore:
        """Wraps the wired store, logging each committed transition."""

        def __init__(self, inner: InMemoryExecutionStore) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        def append_transition(
            self, execution_id: str, transition: object, **kwargs: object
        ):
            log.append(("store", str(transition.to_phase)))  # type: ignore[attr-defined]
            return self._inner.append_transition(execution_id, transition, **kwargs)

    class _RecordingEvents:
        """Wraps the chat event store, logging each appended event."""

        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        def append(self, event: object):
            log.append(("event", str(event.event_type.value)))  # type: ignore[attr-defined]
            return self._inner.append(event)

    recording_store = _RecordingStore(kit.execution_store)
    kit.workflow._execution_store = recording_store  # noqa: SLF001 - test seam
    kit.chat._events = _RecordingEvents(kit.chat._events)  # type: ignore[attr-defined]
    kit.workflow.apply_phase_completion(
        request,
        node_id,
        {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
    )

    phase_event_indices = [
        index for index, entry in enumerate(log) if entry == ("event", "workflow.phase")
    ]
    commit_indices = [
        index
        for index, entry in enumerate(log)
        if entry[0] == "store" and entry[1] in {"falsification_trials", "verdict"}
    ]
    # Store-first ordering: both transitions are committed before the first
    # phase projection is written.
    assert len(commit_indices) == 2
    assert commit_indices[0] < phase_event_indices[0]
    assert commit_indices[1] < phase_event_indices[1]
    assert len(phase_event_indices) == 2


def test_restart_recovery_restores_the_full_typed_state(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    _complete_creation(kit, request)

    # A fresh workflow object over the same store: the in-process orchestrator
    # map is empty, exactly like after a restart.
    fresh = Workflow(
        kit.workflow.spec,
        doctrine=kit.workflow._doctrine,  # noqa: SLF001 - test seam
        services=kit.workflow.services,
        ports=kit.workflow._ports,  # noqa: SLF001 - test seam
    )
    state = fresh._reconstruct_state(request)  # noqa: SLF001 - test seam
    assert state.phase is HypothesisDevelopmentPhase.VERDICT
    assert state.draft is not None
    assert state.draft.claim == _hypothesis_envelope()["claim"]
    assert len(state.trials) == 1
    assert state.trials[0].run_state == "completed"
    assert [item.sequence for item in state.transitions] == [1, 2]

    # The rebuilt state continues the run: the verdict agent's completion
    # applies on top of the store fold.
    active = fresh.ensure_orchestrator(request, state.phase)
    fresh.apply_phase_completion(
        request,
        active.sub_agent_id,
        {
            "verdict": {
                "outcome": "supported",
                "reason": "the planned falsifier survived",
            },
            "target": "strategy_design",
        },
    )
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.STRATEGY_DESIGN.value
    document = kit.execution_store.get(request.execution_id)
    assert [item.to_phase for item in document.transitions] == [
        "falsification_trials",
        "verdict",
        "strategy_design",
    ]
    rebuilt = fresh._reconstruct_state(request)  # noqa: SLF001 - test seam
    assert rebuilt.verdict is not None
    assert rebuilt.verdict.outcome == "supported"
    assert rebuilt.phase is HypothesisDevelopmentPhase.STRATEGY_DESIGN


def test_a_conflicting_launch_keeps_the_existing_document(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    document = kit.execution_store.get(request.execution_id)

    # A replayed launch keeps the existing document instead of clobbering it.
    kit.workflow._record_launch(request)  # noqa: SLF001 - test seam
    assert kit.execution_store.get(request.execution_id) is document

    with pytest.raises(ExecutionStoreConflict):
        kit.execution_store.create(
            ExecutionIdentity(
                execution_id=request.execution_id,
                workspace_id=WORKSPACE,
                chat_id=request.chat_id,
                workflow_id="hypothesis-development",
                workflow_version="1.0.0",
                requested_by="owner-1",
            ),
            launch_request={"ideas": "other"},
        )


def test_a_runnable_workflow_requires_the_execution_store(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    ports = WorkflowPorts(
        chat=kit.chat,
        clock=kit.workflow.clock,
        id_factory=kit.workflow.id_factory,
        conversations=kit.conversations,
    )
    with pytest.raises(ValueError, match="execution store"):
        Workflow(
            kit.workflow.spec,
            doctrine=kit.workflow._doctrine,  # noqa: SLF001 - test seam
            services=kit.workflow.services,
            ports=ports,
        )


def test_the_trials_datum_survives_as_a_store_record(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    _complete_creation(kit, request)

    document = kit.execution_store.get(request.execution_id)
    # The trials datum lives in the trials→verdict completion contract.
    trials = document.transitions[-1].completion["trials"]
    assert trials[0]["observed"] == 1.0
    # And the phase's rendered result artifact is a store record too (REQ-B1).
    artifact = document.phase_results["falsification_trials"]
    assert artifact["type"] == "falsification_report"
    assert artifact["totalCount"] == 1
