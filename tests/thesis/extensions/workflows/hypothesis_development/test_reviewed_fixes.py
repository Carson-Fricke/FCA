"""Accepted payload parity and deterministic terminal failure regressions."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.thesis.extensions.workflows.hypothesis_development._support import (
    _phase_attempt,
)

from thesis.extensions.workflows.hypothesis_development.contracts.validation import (
    HYPOTHESIS_PAYLOAD_SCHEMA,
    hypothesis_payload_errors,
)
from thesis.extensions.workflows.hypothesis_development.endpoint import (
    HypothesisExtension,
)
from thesis.extensions.workflows.hypothesis_development.phases import (
    HypothesisDevelopmentPhase,
)
from thesis.extensions.workflows.hypothesis_development.rebuild import rebuild_state
from thesis.workflows.backtests import BacktestRunError
from thesis_runtime.tools.schema import violations
from thesis_runtime.workflows.execution_store import StoreTransition
from thesis_runtime.workflows.workflow import WorkflowEngineError

from ._support import (
    WORKSPACE,
    _discovery_envelope,
    _hypothesis_envelope,
    _kit,
    _phase_node_id,
    _request,
    _research_envelope,
    _sketch_reference,
)


def test_retired_indicators_are_rejected_by_the_advertised_schema() -> None:
    payload = _hypothesis_envelope()
    model = payload["model"]
    assert isinstance(model, dict)
    model["indicators"] = []
    misses = violations(HYPOTHESIS_PAYLOAD_SCHEMA, payload, "hypothesis")
    assert any(miss.path == "hypothesis.model.indicators" for miss in misses)
    assert hypothesis_payload_errors(payload) == [miss.render() for miss in misses]


def test_live_discovery_state_matches_replay_before_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    projected = []
    original = HypothesisExtension.invoke
    projected = []

    def invoke(endpoint, operation, payload):
        if operation == "report":
            projected.append(
                kit.extension.state(
                    request,
                    tuple(
                        StoreTransition.from_dict(item) for item in payload["history"]
                    ),
                )
            )
        return original(endpoint, operation, payload)

    monkeypatch.setattr(HypothesisExtension, "invoke", invoke)
    kit.workflow.apply_phase_completion(
        request,
        node,
        {
            "target": "model_creation",
            "discovery": _discovery_envelope(),
        },
        attempt_id=_phase_attempt(kit.workflow, request.workspace_id, node),
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
    monkeypatch: pytest.MonkeyPatch,
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
        attempt_id=_phase_attempt(kit.workflow, request.workspace_id, node),
    )
    if detached:
        work.pop(0)()
    original = HypothesisExtension.invoke

    def invoke(endpoint, operation, payload):
        if operation == "execute":
            if invalid_contract:
                return {"completion": {"trials": []}}
            raise BacktestRunError("study polling timed out")
        return original(endpoint, operation, payload)

    monkeypatch.setattr(HypothesisExtension, "invoke", invoke)
    completion = {
        "target": "falsification_trials",
        "hypothesis": _hypothesis_envelope(),
    }
    node = _phase_node_id(kit, request, "model_creation")
    if detached:
        kit.workflow.apply_phase_completion(
            request,
            node,
            completion,
            attempt_id=_phase_attempt(kit.workflow, request.workspace_id, node),
        )
        work.pop(0)()
    else:
        with pytest.raises(
            (BacktestRunError, WorkflowEngineError), match="timed out|invalid contract"
        ):
            kit.workflow.apply_phase_completion(
                request,
                node,
                completion,
                attempt_id=_phase_attempt(kit.workflow, request.workspace_id, node),
            )
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


def test_deterministic_missing_contract_fails_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    kit.workflow.apply_phase_completion(
        request,
        node,
        {"target": "model_creation", "discovery": _discovery_envelope()},
        attempt_id=_phase_attempt(kit.workflow, request.workspace_id, node),
    )
    original = HypothesisExtension.invoke

    def invoke(endpoint, operation, payload):
        return (
            {"completion": None}
            if operation == "execute"
            else original(endpoint, operation, payload)
        )

    monkeypatch.setattr(HypothesisExtension, "invoke", invoke)
    with pytest.raises(WorkflowEngineError, match="no contract"):
        kit.workflow.apply_phase_completion(
            request,
            _phase_node_id(kit, request, "model_creation"),
            {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
            attempt_id=_phase_attempt(
                kit.workflow,
                request.workspace_id,
                _phase_node_id(kit, request, "model_creation"),
            ),
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
    state = kit.extension.state(
        request, kit.execution_store.get((request).execution_id).transitions
    )
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
        {
            "target": "verdict",
            "trials": [trial],
            "research": _research_envelope(),
        },
        {
            "target": target,
            "verdict": {
                "outcome": "supported",
                "reason": "review accepted",
                "sketchReference": _sketch_reference(),
                "assumptionAssessments": [
                    {
                        "assumptionId": "continuation",
                        "status": "supported",
                        "reason": "Trial",
                        "testIds": ["t1"],
                    }
                ],
            },
        },
    ]
    history = []
    for completion in completions:
        history.append(
            StoreTransition(
                len(history) + 1,
                state.phase.value,
                str(completion["target"]),
                datetime(2026, 1, 1, tzinfo=UTC),
                0,
                "accepted",
                completion,
                attempt_id="fixture-attempt",
            )
        )
        state = rebuild_state(WORKSPACE, request.execution_id, history)
        rebuilt = rebuild_state(
            WORKSPACE,
            request.execution_id,
            tuple(StoreTransition.from_dict(item.as_dict()) for item in history),
        )
        assert state == rebuilt
