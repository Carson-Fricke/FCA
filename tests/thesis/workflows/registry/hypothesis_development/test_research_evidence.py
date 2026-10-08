"""The trials evidence envelope: decode, fold, and drift-guard rejection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from thesis.workflows.core.execution_store import StoreTransition
from thesis.workflows.registry.hypothesis_development.contracts.evidence import (
    ControlEvidence,
    ResearchEvidence,
)
from thesis.workflows.registry.hypothesis_development.errors import WorkflowError
from thesis.workflows.registry.hypothesis_development.phases import (
    HypothesisDevelopmentPhase,
)
from thesis.workflows.registry.hypothesis_development.rebuild import (
    fold_transition,
    rebuild_state,
)
from thesis.workflows.registry.hypothesis_development.state import (
    HypothesisDevelopmentState,
)

from ._support import (
    WORKSPACE,
    _discovery_envelope,
    _hypothesis_envelope,
    _research_envelope,
)

_TRIAL = {
    "testId": "T01",
    "falsifierId": "f1",
    "jobId": "job-1",
    "digest": "a" * 64,
    "runState": "succeeded",
    "observed": 1.0,
    "falsified": False,
}


def _moment() -> datetime:
    return datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _discovery_transition() -> StoreTransition:
    return StoreTransition(
        sequence=1,
        from_phase="discovery",
        to_phase="model_creation",
        occurred_at=_moment(),
        intent_epoch=1,
        reason="discovery candidate accepted",
        completion={
            "target": "model_creation",
            "discovery": _discovery_envelope(),
        },
    )


def _model_transition() -> StoreTransition:
    return StoreTransition(
        sequence=2,
        from_phase="model_creation",
        to_phase="falsification_trials",
        occurred_at=_moment(),
        intent_epoch=1,
        reason="accepted model",
        completion={"hypothesis": _hypothesis_envelope()},
    )


def _trials_transition(
    sequence: int = 3,
    *,
    research: dict[str, object] | None = None,
) -> StoreTransition:
    completion: dict[str, object] = {
        "target": "verdict",
        "trials": [_TRIAL],
    }
    if research is not None:
        completion["research"] = research
    return StoreTransition(
        sequence=sequence,
        from_phase="falsification_trials",
        to_phase="verdict",
        occurred_at=_moment(),
        intent_epoch=1,
        reason="accepted trials",
        completion=completion,
    )


def test_envelope_round_trips_and_folds_after_pure_rebuild() -> None:
    state = rebuild_state(
        WORKSPACE,
        "execution-evidence",
        [
            _discovery_transition(),
            _model_transition(),
            _trials_transition(research=_research_envelope()),
        ],
    )

    assert state.phase is HypothesisDevelopmentPhase.VERDICT
    evidence = state.research_evidence
    assert isinstance(evidence, ResearchEvidence)
    assert ResearchEvidence.from_dict(evidence.as_dict()) == evidence
    assert [item.statement for item in evidence.assumptions] == [
        "Run the momentum probe and require a negative Sharpe."
    ]
    assert [item.role for item in evidence.assumptions] == ["claim"]
    assert evidence.controls == ()
    assert evidence.report_digest == "d" * 64

    # The fold restores the plan and every run-derived state field the trials
    # phase had, so the replay state equals the live state.
    assert state.plan is not None
    assert [test.test_id for test in state.plan.tests] == ["T01"]
    assert state.research_model_reference is not None
    assert state.research_model_reference.model_id == "momentum-model"
    assert state.research_model_artifact == "workspace-model:momentum-model"
    assert state.fitted_model_reference is not None
    assert state.fitted_model_reference.fit_sha256 == "b" * 64
    assert state.fitted_parameters == {"baseline": 0.5}
    assert state.research_model_runtime_configuration == {"sourceSha256": "c" * 64}
    assert [trial.test_id for trial in state.trials] == ["T01"]


def test_legacy_trials_completion_without_envelope_still_folds() -> None:
    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-legacy-trials",
        phase=HypothesisDevelopmentPhase.FALSIFICATION_TRIALS,
        intent_epoch=0,
    )

    folded = fold_transition(state, _trials_transition(sequence=1, research=None))

    assert folded.phase is HypothesisDevelopmentPhase.VERDICT
    assert folded.research_evidence is None
    assert [trial.test_id for trial in folded.trials] == ["T01"]


def test_non_finite_fitted_parameters_are_rejected_by_decode() -> None:
    envelope = _research_envelope()
    envelope["fittedParameters"] = {"baseline": float("nan")}
    with pytest.raises(WorkflowError, match="finite"):
        ResearchEvidence.from_dict(envelope)

    envelope = _research_envelope()
    envelope["fittedParameters"] = {"baseline": True}
    with pytest.raises(WorkflowError, match="finite"):
        ResearchEvidence.from_dict(envelope)


def test_malformed_envelope_components_are_rejected() -> None:
    envelope = _research_envelope()
    envelope["reportDigest"] = "not-a-digest"
    with pytest.raises(WorkflowError, match="digest"):
        ResearchEvidence.from_dict(envelope)

    envelope = _research_envelope()
    envelope["study"] = {"programId": "momentum"}
    with pytest.raises(WorkflowError, match="study reference"):
        ResearchEvidence.from_dict(envelope)

    envelope = _research_envelope()
    envelope["assumptions"] = [{"falsifierId": "f1"}]
    with pytest.raises(WorkflowError):
        ResearchEvidence.from_dict(envelope)

    envelope = _research_envelope()
    envelope["controls"] = [
        {"controlId": "c1", "description": "Shuffle labels.", "expectation": "Vanish."}
    ]
    evidence = ResearchEvidence.from_dict(envelope)
    assert [control.control_id for control in evidence.controls] == ["c1"]


def test_control_records_carry_id_description_and_expectation() -> None:
    control = ControlEvidence(
        control_id="c1",
        description="Run the probe on shuffled labels.",
        expectation="The edge should vanish.",
    )
    assert control.as_dict() == {
        "controlId": "c1",
        "description": "Run the probe on shuffled labels.",
        "expectation": "The edge should vanish.",
    }
    assert ControlEvidence.from_dict(control.as_dict()) == control
