"""Accepted payload parity and deterministic terminal failure regressions."""

from dataclasses import replace
from pathlib import Path

import pytest

from thesis.agent.runtime.schema import violations
from thesis.workflows.backtests import BacktestRunError
from thesis.workflows.core.phase import DeterministicPhase
from thesis.workflows.core.workflow import WorkflowEngineError
from thesis.workflows.registry.hypothesis_development.contracts.validation import (
    HYPOTHESIS_PAYLOAD_SCHEMA,
    hypothesis_payload_errors,
)
from thesis.workflows.registry.hypothesis_development.phases import (
    HypothesisDevelopmentPhase,
)
from thesis.workflows.registry.hypothesis_development.rebuild import rebuild_state

from ._support import (
    WORKSPACE,
    _discovery_envelope,
    _hypothesis_envelope,
    _kit,
    _phase_node_id,
    _request,
)


def test_retired_indicators_are_rejected_by_the_advertised_schema() -> None:
    payload = _hypothesis_envelope()
    model = payload["model"]
    assert isinstance(model, dict)
    model["indicators"] = []
    misses = violations(HYPOTHESIS_PAYLOAD_SCHEMA, payload, "hypothesis")
    assert any(miss.path == "hypothesis.model.indicators" for miss in misses)
    assert hypothesis_payload_errors(payload) == [miss.render() for miss in misses]


def test_live_discovery_state_matches_replay_before_projection(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    projected = []
    doctrine = kit.workflow._doctrine
    assert doctrine is not None
    original_report = doctrine.report

    def report(workflow, state, active):
        projected.append(state)
        return original_report(workflow, state, active)

    kit.workflow._doctrine = replace(doctrine, report=report)
    kit.workflow.apply_phase_completion(
        request,
        node,
        {
            "target": "model_creation",
            "discovery": _discovery_envelope(),
        },
    )
    live = projected[0]
    rebuilt = rebuild_state(
        WORKSPACE,
        request.execution_id,
        kit.execution_store.get(request.execution_id).transitions,
    )
    assert live == rebuilt
    assert live.discovery is not None
    assert live.transitions == rebuilt.transitions


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("invalid_contract", [False, True])
def test_deterministic_fault_is_terminal_not_repairable(
    tmp_path: Path,
    detached: bool,
    invalid_contract: bool,
) -> None:
    work = []
    kit = _kit(
        tmp_path, payloads=[], results=[], detach=work.append if detached else None
    )
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    kit.workflow.apply_phase_completion(
        request,
        node,
        {
            "target": "model_creation",
            "discovery": _discovery_envelope(),
        },
    )
    if detached:
        work.pop(0)()
    phase = kit.workflow.spec.phase_specs["falsification_trials"]
    assert isinstance(phase, DeterministicPhase)

    def contract(*args):
        if invalid_contract:
            return {"trials": []}
        raise BacktestRunError("study polling timed out")

    kit.workflow._spec = replace(
        kit.workflow.spec,
        phases=tuple(
            replace(item, contract=contract) if item is phase else item
            for item in kit.workflow.spec.phases
        ),
    )
    completion = {
        "target": "falsification_trials",
        "hypothesis": _hypothesis_envelope(),
    }
    node = _phase_node_id(kit, request, "model_creation")
    if detached:
        kit.workflow.apply_phase_completion(request, node, completion)
        work.pop(0)()
    else:
        with pytest.raises(
            (BacktestRunError, WorkflowEngineError), match="timed out|invalid contract"
        ):
            kit.workflow.apply_phase_completion(request, node, completion)
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state.value == "failed"
    assert execution.finished_at is not None
    assert execution.phase == HypothesisDevelopmentPhase.FALSIFICATION_TRIALS.value
    failures = [
        message
        for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
        if message.data and message.data.get("datumType") == "workflow_outcome"
    ]
    assert len(failures) == 1
    kit.workflow.fail_execution(request, reason="late duplicate")
    assert (
        len(
            [
                message
                for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
                if message.data and message.data.get("datumType") == "workflow_outcome"
            ]
        )
        == 1
    )


def test_deterministic_missing_contract_fails_workflow(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    kit.workflow.apply_phase_completion(
        request, node, {"target": "model_creation", "discovery": _discovery_envelope()}
    )
    phase = kit.workflow.spec.phase_specs["falsification_trials"]
    assert isinstance(phase, DeterministicPhase)
    kit.workflow._spec = replace(
        kit.workflow.spec,
        phases=tuple(
            replace(item, contract=lambda *args: None) if item is phase else item
            for item in kit.workflow.spec.phases
        ),
    )
    with pytest.raises(WorkflowEngineError, match="no contract"):
        kit.workflow.apply_phase_completion(
            request,
            _phase_node_id(kit, request, "model_creation"),
            {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
        )
    assert (
        kit.chat.get_execution(WORKSPACE, request.execution_id).state.value == "failed"
    )


@pytest.mark.parametrize("target", ["model_creation", "discovery", "strategy_design"])
def test_live_accepted_contract_sequence_matches_replay(
    tmp_path: Path, target: str
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    state = kit.workflow._reconstruct_state(request)
    trial = {
        "testId": "t1",
        "falsifierId": "f1",
        "jobId": "job-1",
        "digest": "a" * 64,
        "runState": "succeeded",
        "observed": 1.0,
        "falsified": False,
    }
    completions = [
        {"target": "model_creation", "discovery": _discovery_envelope()},
        {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
        {"target": "verdict", "trials": [trial]},
        {
            "target": target,
            "verdict": {"outcome": "supported", "reason": "review accepted"},
        },
    ]
    for completion in completions:
        state = kit.workflow._apply_contract(
            request, state, state.phase.value, completion
        )
        rebuilt = rebuild_state(
            WORKSPACE,
            request.execution_id,
            kit.execution_store.get(request.execution_id).transitions,
        )
        assert state == rebuilt
