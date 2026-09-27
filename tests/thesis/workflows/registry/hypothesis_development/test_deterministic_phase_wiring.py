"""Deterministic-phase de-noding (Part C, Phase 2).

The falsification-trials phase is a computation, not a sub-agent: no durable
node, no orchestrator lifecycle, no ``sub_agent.*`` events.  Its progress and
outcome are execution-scoped ``workflow.progress`` / ``workflow.phase_result``
events carrying the phase name, and a failed computation records the error
variant before propagating.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat.contracts import WorkflowExecutionState
from thesis.execution.events import ExecutionEventType
from thesis.research.models import ResearchModelRef
from thesis.workflows.backtests import BacktestRunResult
from thesis.workflows.registry.hypothesis_development import (
    HypothesisDevelopmentPhase,
)
from thesis.workflows.research_models import ResearchModelRunResult

from ._support import WORKSPACE, _events_by_type, _hypothesis_envelope, _kit, _request

TRIALS = "falsification_trials"


class _StudyRunner:
    """A research-model port returning one fixed study run."""

    def __init__(
        self,
        *,
        metrics: object | None,
        state: str = "completed",
        error: str | None = None,
    ) -> None:
        self._metrics = metrics
        self._state = state
        self._error = error

    def run(self, request: object) -> ResearchModelRunResult:
        del request
        return ResearchModelRunResult(
            model_reference=ResearchModelRef(
                workspace_id=WORKSPACE,
                model_id="momentum-model",
                content_sha256="a" * 64,
            ),
            backtest=BacktestRunResult(
                job_id="study-1",
                state=self._state,
                metrics=self._metrics,
                error=self._error,
            ),
            workspace_artifact="workspace-model:momentum-model",
            runtime_configuration={},
            fitted_reference=None,
        )


class _CompletedStudyRunner(_StudyRunner):
    """A research-model port returning one completed study run."""

    def __init__(self) -> None:
        super().__init__(metrics={"sharpe": 1.0, "sample_valid": 1.0})


class _ExplodingStudyRunner:
    """A research-model port whose computation fails outright."""

    def run(self, request: object) -> ResearchModelRunResult:
        raise RuntimeError("harness exploded")


def _complete_creation(kit, request: object) -> None:
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    kit.workflow.apply_phase_completion(
        request,
        node_id,
        {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
    )


def test_no_trials_record_exists_in_the_sub_agent_tree(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    _complete_creation(kit, request)

    records = PersistenceSubAgentStore(kit.chat.persistence).list_sub_agents(WORKSPACE)
    assert records
    assert all(record.phase != TRIALS for record in records)
    spawns = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_SPAWNED
    )
    assert spawns
    assert all(event.payload.get("phase") != TRIALS for event in spawns)
    # No sub_agent.* event names the trials phase at all.
    events = kit.workflow.observe(WORKSPACE, request.execution_id).events
    assert not [
        event
        for event in events
        if event.event_type.value.startswith("sub_agent.")
        and event.payload.get("phase") == TRIALS
    ]


def test_trials_progress_is_execution_scoped(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    _complete_creation(kit, request)

    progress = _events_by_type(
        kit, request.execution_id, ExecutionEventType.WORKFLOW_PROGRESS
    )
    assert progress, "the falsification run recorded no progress"
    for event in progress:
        assert event.payload["phase"] == TRIALS
        assert "subAgentId" not in event.payload
        assert event.actor_scope.value == "workflow"
    started = [
        event
        for event in progress
        if "Running the falsification model" in str(event.payload.get("text", ""))
    ]
    completed = [
        event
        for event in progress
        if "completed in state completed" in str(event.payload.get("text", ""))
    ]
    assert started and completed


def test_the_trials_result_is_a_workflow_scoped_event_and_store_record(
    tmp_path: Path,
) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    _complete_creation(kit, request)

    results_events = _events_by_type(
        kit, request.execution_id, ExecutionEventType.WORKFLOW_PHASE_RESULT
    )
    assert len(results_events) == 1
    event = results_events[0]
    assert event.payload["phase"] == TRIALS
    assert "subAgentId" not in event.payload
    artifact = event.payload["artifact"]
    assert artifact["type"] == "falsification_report"
    assert artifact["totalCount"] == 1
    assert artifact["passedCount"] == 1
    assert event.payload["summary"]
    # The typed artifact is also a store record (REQ-B1).
    document = kit.execution_store.get(request.execution_id)
    assert document.phase_results[TRIALS]["type"] == "falsification_report"

    # The trials cascade advanced into the verdict phase, whose agent now runs.
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.VERDICT.value


def test_a_failed_computation_records_the_error_variant(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_ExplodingStudyRunner()
    )
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)

    import pytest

    with pytest.raises(RuntimeError, match="harness exploded"):
        kit.workflow.apply_phase_completion(
            request,
            node_id,
            {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
        )

    results_events = _events_by_type(
        kit, request.execution_id, ExecutionEventType.WORKFLOW_PHASE_RESULT
    )
    assert len(results_events) == 1
    event = results_events[0]
    assert event.payload["phase"] == TRIALS
    assert event.payload["artifact"] == {
        "type": "error",
        "message": "harness exploded",
    }
    # The creation→trials transition committed; the cascade stopped there.
    document = kit.execution_store.get(request.execution_id)
    assert [item.to_phase for item in document.transitions] == [TRIALS]
    assert document.phase_results[TRIALS]["type"] == "error"
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == TRIALS


def test_the_trials_artifact_renders_for_the_operator_view(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    _complete_creation(kit, request)

    results_events = _events_by_type(
        kit, request.execution_id, ExecutionEventType.WORKFLOW_PHASE_RESULT
    )
    artifact = results_events[0].payload["artifact"]
    assert artifact["hypothesis"]["claim"] == _hypothesis_envelope()["claim"]
    assert artifact["survived"] is True
    trial = artifact["trials"][0]
    assert trial["testId"]
    assert trial["observed"] == 1.0


def test_a_data_failure_falsifier_records_its_actual_observation(
    tmp_path: Path,
) -> None:
    """A falsifier on a data-failure observable (``data_ok``) observes its real
    metric value and fires legitimately; there is no collapse into no
    observation and no silent pass."""
    falsifier: dict[str, object] = {
        "falsifierId": "f1",
        "observable": "data_ok",
        "condition": "lt",
        "threshold": 1.0,
        "test": "Require complete input data for the estimation window.",
    }
    kit = _kit(
        tmp_path,
        payloads=[],
        results=[],
        research_runner=_StudyRunner(metrics={"sharpe": 1.0, "data_ok": 0.0}),
    )
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    kit.workflow.apply_phase_completion(
        request,
        node_id,
        {
            "target": "falsification_trials",
            "hypothesis": _hypothesis_envelope(falsifiers=[falsifier]),
        },
    )

    results_events = _events_by_type(
        kit, request.execution_id, ExecutionEventType.WORKFLOW_PHASE_RESULT
    )
    assert len(results_events) == 1
    artifact = results_events[0].payload["artifact"]
    assert artifact["type"] == "falsification_report"
    trial = artifact["trials"][0]
    assert trial["observed"] == 0.0
    assert trial["falsified"] is True
    assert artifact["survived"] is False
    document = kit.execution_store.get(request.execution_id)
    assert document.transitions[-1].to_phase == "verdict"


def test_a_missing_observable_fails_the_phase(tmp_path: Path) -> None:
    """A planned falsifier whose observable is absent from the study metrics is
    the failure condition: the phase records the error variant and raises."""
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    missing: dict[str, object] = {
        "falsifierId": "f1",
        "observable": "hurst",
        "condition": "gt",
        "threshold": 0.5,
        "test": "Require a Hurst exponent above the random-walk baseline.",
    }

    with pytest.raises(RuntimeError, match="do not carry the planned"):
        kit.workflow.apply_phase_completion(
            request,
            node_id,
            {
                "target": "falsification_trials",
                "hypothesis": _hypothesis_envelope(falsifiers=[missing]),
            },
        )

    results_events = _events_by_type(
        kit, request.execution_id, ExecutionEventType.WORKFLOW_PHASE_RESULT
    )
    assert len(results_events) == 1
    event = results_events[0]
    assert event.payload["artifact"]["type"] == "error"
    assert "hurst" in event.payload["artifact"]["message"]
    document = kit.execution_store.get(request.execution_id)
    assert document.phase_results[TRIALS]["type"] == "error"
    assert [item.to_phase for item in document.transitions] == [TRIALS]


def test_empty_study_metrics_fail_the_phase(tmp_path: Path) -> None:
    """An empty or malformed study result is the failure condition."""
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_StudyRunner(metrics=None)
    )
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None

    with pytest.raises(RuntimeError, match="no usable metrics"):
        kit.workflow.apply_phase_completion(
            request,
            node_id,
            {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
        )

    results_events = _events_by_type(
        kit, request.execution_id, ExecutionEventType.WORKFLOW_PHASE_RESULT
    )
    assert len(results_events) == 1
    assert results_events[0].payload["artifact"]["type"] == "error"


def test_a_failed_study_run_fails_the_phase_with_the_harness_detail(
    tmp_path: Path,
) -> None:
    """A harness-reported study failure surfaces through the error variant
    with its detail; no silent all-failed-trial completion is recorded."""
    kit = _kit(
        tmp_path,
        payloads=[],
        results=[],
        research_runner=_StudyRunner(
            metrics={"sharpe": 1.0},
            state="failed",
            error="the harness engine crashed during replay",
        ),
    )
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None

    with pytest.raises(RuntimeError, match="harness engine crashed"):
        kit.workflow.apply_phase_completion(
            request,
            node_id,
            {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
        )

    results_events = _events_by_type(
        kit, request.execution_id, ExecutionEventType.WORKFLOW_PHASE_RESULT
    )
    assert len(results_events) == 1
    event = results_events[0]
    assert event.payload["artifact"] == {
        "type": "error",
        "message": (
            "the falsification study failed: the harness engine crashed during replay"
        ),
    }
    document = kit.execution_store.get(request.execution_id)
    assert [item.to_phase for item in document.transitions] == [TRIALS]
    assert document.phase_results[TRIALS]["type"] == "error"


class _CapturedDetach:
    """A detach seam that queues the out-of-band callables for the test."""

    def __init__(self) -> None:
        self.calls: list[Callable[[], None]] = []

    def __call__(self, function: Callable[[], None]) -> None:
        self.calls.append(function)

    def run_all(self) -> None:
        while self.calls:
            self.calls.pop(0)()


def test_a_completion_returns_at_acceptance_and_the_trials_run_out_of_band(
    tmp_path: Path,
) -> None:
    """The completion tool call ends when its transition commits; the
    deterministic successor is its own unit of work.  The accepted call must
    not sit open while the trials fetch data (the 2026-09-23 incident)."""
    detached = _CapturedDetach()
    kit = _kit(
        tmp_path,
        payloads=[],
        results=[],
        research_runner=_CompletedStudyRunner(),
        detach=detached,
    )
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None

    applied = kit.workflow.apply_phase_completion(
        request,
        node_id,
        {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
    )

    # The tool-facing answer is the accepted transition; nothing of the
    # successor phase has run yet.
    assert applied["phase"] == TRIALS
    document = kit.execution_store.get(request.execution_id)
    assert [transition.to_phase for transition in document.transitions] == [TRIALS]
    assert TRIALS not in document.phase_results
    assert len(detached.calls) == 1

    # Entering the successor is its own unit of work.
    detached.run_all()

    document = kit.execution_store.get(request.execution_id)
    assert [transition.to_phase for transition in document.transitions] == [
        TRIALS,
        "verdict",
    ]
    assert document.phase_results[TRIALS]["type"] == "falsification_report"


def test_a_detached_phase_fault_fails_the_execution_durably(tmp_path: Path) -> None:
    """With the phase work out of band there is no caller to raise into, so a
    fault becomes the execution's terminal record instead of a wedged run."""
    detached = _CapturedDetach()
    kit = _kit(
        tmp_path,
        payloads=[],
        results=[],
        research_runner=_StudyRunner(metrics=None),
        detach=detached,
    )
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None

    applied = kit.workflow.apply_phase_completion(
        request,
        node_id,
        {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
    )
    assert applied["phase"] == TRIALS

    detached.run_all()

    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state == WorkflowExecutionState.FAILED
    assert _events_by_type(
        kit, request.execution_id, ExecutionEventType.WORKFLOW_FAILED
    )
