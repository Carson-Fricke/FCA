"""The discovery phase: capabilities, contracts, pipes, and folds."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from thesis.agent.runtime.schema import validate_arguments
from thesis.workflows.core.execution_store import StoreTransition
from thesis.workflows.registry.hypothesis_development.contracts.discovery import (
    DiscoveryDraft,
)
from thesis.workflows.registry.hypothesis_development.contracts.validation import (
    DISCOVERY_COMPLETION_SCHEMA,
    discovery_payload_errors,
)
from thesis.workflows.registry.hypothesis_development.errors import WorkflowError
from thesis.workflows.registry.hypothesis_development.phases import (
    HypothesisDevelopmentPhase,
    phase_from_value,
)
from thesis.workflows.registry.hypothesis_development.piping import (
    identity_pipe,
    reset_backtests,
    reset_discovery,
    reset_premise,
)
from thesis.workflows.registry.hypothesis_development.rebuild import (
    fold_transition,
    rebuild_state,
)
from thesis.workflows.registry.hypothesis_development.spec import (
    build_hypothesis_development_workflow,
)
from thesis.workflows.registry.hypothesis_development.spec import (
    piping as workflow_piping,
)
from thesis.workflows.registry.hypothesis_development.state import (
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentState,
)

from ._support import (
    WORKSPACE,
    _discovery_envelope,
    _hypothesis_envelope,
    _no_candidate_discovery_envelope,
    _research_envelope,
)


def _moment() -> datetime:
    return datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _discovery_transition(
    sequence: int = 1,
) -> StoreTransition:
    return StoreTransition(
        sequence=sequence,
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


def _verdict_transition(
    sequence: int, target: str, outcome: str = "refuted"
) -> StoreTransition:
    return StoreTransition(
        sequence=sequence,
        from_phase="verdict",
        to_phase=target,
        occurred_at=_moment(),
        intent_epoch=2,
        reason="verdict loop-back accepted",
        completion={
            "target": target,
            "verdict": {
                "outcome": outcome,
                "reason": "the planned falsifier fired",
            },
        },
    )


def test_discovery_completion_schema_and_validator_reject_missing_pieces() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["discovery"]
    assert phase.completion_schema is DISCOVERY_COMPLETION_SCHEMA
    assert phase.validate is not None

    completion = {"target": "model_creation", "discovery": _discovery_envelope()}
    assert not validate_arguments(phase.completion_schema, completion)
    assert phase.validate(completion) is None

    missing_candidate = _discovery_envelope()
    del missing_candidate["sketchReference"]
    failure = phase.validate(
        {"target": "model_creation", "discovery": missing_candidate}
    )
    assert failure is not None
    assert "discovery.sketchReference" in failure
    assert validate_arguments(
        phase.completion_schema,
        {"target": "model_creation", "discovery": missing_candidate},
    )

    missing_attempts = _discovery_envelope()
    del missing_attempts["attempts"]
    failure = phase.validate(
        {"target": "model_creation", "discovery": missing_attempts}
    )
    assert failure is not None
    assert "discovery.attempts" in failure

    empty_attempts = _discovery_envelope()
    empty_attempts["attempts"] = []
    failure = phase.validate({"target": "model_creation", "discovery": empty_attempts})
    assert failure is not None
    assert "expected at least 1 item(s)" in failure
    assert discovery_payload_errors(empty_attempts)

    missing_payload = phase.validate({"target": "model_creation"})
    assert missing_payload is not None
    assert "discovery" in missing_payload


def test_accepted_discovery_contract_folds_into_state() -> None:
    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-discovery",
        phase=HypothesisDevelopmentPhase.DISCOVERY,
        intent_epoch=0,
    )

    folded = fold_transition(state, _discovery_transition())

    assert folded.phase is HypothesisDevelopmentPhase.MODEL_CREATION
    assert folded.discovery is not None
    assert folded.discovery.sketch_reference is not None
    assert folded.discovery.sketch_reference.sketch_id == "momentum-edge"
    assert [attempt.job_id for attempt in folded.discovery.attempts] == ["job-momentum"]
    assert folded.hypothesis is None
    assert folded.draft is None
    assert folded.trials == ()


def test_registered_pipes_keep_or_reset_discovery_as_designed() -> None:
    pipes = workflow_piping()
    assert pipes[("discovery", "model_creation")] is reset_premise
    assert pipes[("model_creation", "falsification_trials")] is identity_pipe
    assert pipes[("verdict", "model_creation")] is reset_premise
    assert pipes[("verdict", "discovery")] is reset_discovery

    draft = DiscoveryDraft.from_dict(_discovery_envelope())
    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-pipes",
        phase=HypothesisDevelopmentPhase.VERDICT,
        intent_epoch=0,
        discovery=draft,
        research_model_artifact="workspace-model:momentum-model",
        fitted_parameters={"fit.baseline": 0.5},
    )

    kept = pipes[("verdict", "model_creation")](state)
    assert kept.discovery is draft
    assert kept.research_model_artifact is None
    assert kept.fitted_parameters is None

    cleared = pipes[("verdict", "discovery")](state)
    assert cleared.discovery is None
    assert cleared.research_model_artifact is None
    assert cleared.fitted_parameters is None

    # The prescription only touches strategy backtests.
    assert pipes[("strategy_rca", "strategy_design")] is reset_backtests


def test_verdict_loopback_folds_keep_or_clear_discovery() -> None:
    prefix = [
        _discovery_transition(),
        StoreTransition(
            sequence=2,
            from_phase="model_creation",
            to_phase="falsification_trials",
            occurred_at=_moment(),
            intent_epoch=1,
            reason="accepted model",
            completion={"hypothesis": _hypothesis_envelope()},
        ),
        StoreTransition(
            sequence=3,
            from_phase="falsification_trials",
            to_phase="verdict",
            occurred_at=_moment(),
            intent_epoch=1,
            reason="accepted trials",
            completion={"trials": [], "research": _research_envelope()},
        ),
    ]
    kept = rebuild_state(
        WORKSPACE,
        "execution-keep",
        [*prefix, _verdict_transition(4, "model_creation")],
    )
    assert kept.phase is HypothesisDevelopmentPhase.MODEL_CREATION
    assert kept.discovery is not None
    assert kept.verdict is not None and kept.verdict.outcome == "refuted"
    assert kept.draft is None
    assert kept.trials == ()

    cleared = rebuild_state(
        WORKSPACE,
        "execution-clear",
        [*prefix, _verdict_transition(4, "discovery")],
    )
    assert cleared.phase is HypothesisDevelopmentPhase.DISCOVERY
    assert cleared.discovery is None
    assert cleared.verdict is not None and cleared.verdict.outcome == "refuted"


def test_retired_phase_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="unknown phase"):
        phase_from_value("hypothesis_creation")


def test_discovery_attempt_uses_an_optional_job_identity_without_study_id() -> None:
    payload = _discovery_envelope()
    attempts = payload["attempts"]
    assert isinstance(attempts, list)
    attempt = attempts[0]
    assert isinstance(attempt, dict)
    del attempt["jobId"]

    draft = DiscoveryDraft.from_dict(payload)
    assert draft.attempts[0].job_id is None
    assert discovery_payload_errors(payload) == []
    assert "studyId" not in draft.attempts[0].as_dict()
    assert "runId" not in draft.attempts[0].as_dict()

    attempt["jobId"] = "job-momentum"
    assert DiscoveryDraft.from_dict(payload).attempts[0].job_id == "job-momentum"


def test_discovery_draft_round_trips_and_rejects_malformed_payloads() -> None:
    draft = DiscoveryDraft.from_dict(_discovery_envelope())
    assert DiscoveryDraft.from_dict(draft.as_dict()) == draft
    candidate = draft.as_dict()["sketchReference"]
    assert isinstance(candidate, dict)
    assert candidate["gitCommit"]

    outcome = HypothesisDevelopmentOutcome(
        workspace_id=WORKSPACE,
        execution_id="execution-transport",
        phase=HypothesisDevelopmentPhase.MODEL_CREATION,
        fenced=False,
        transitions=(),
        discovery=draft,
    )
    payload = outcome.as_dict()
    assert payload["discovery"] == draft.as_dict()
    # Existing outcome keys survive beside the new one.
    assert payload["phase"] == "model_creation"
    assert "hypothesis" in payload and "trials" in payload


def test_discovery_no_candidate_branch_and_target_consistency() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["discovery"]
    assert phase.validate is not None
    no_candidate = _no_candidate_discovery_envelope()
    completion = {"target": "completed", "discovery": no_candidate}
    assert not validate_arguments(phase.completion_schema, completion)
    assert discovery_payload_errors(no_candidate) == []
    assert phase.validate(completion) is None

    wrong_target = {"target": "model_creation", "discovery": no_candidate}
    failure = phase.validate(wrong_target)
    assert failure is not None and "completed" in failure

    wrong_candidate = {"target": "completed", "discovery": _discovery_envelope()}
    failure = phase.validate(wrong_candidate)
    assert failure is not None and "model_creation" in failure

    # The terminal failure escape stays available for either branch.
    assert phase.validate({"target": "failed", "discovery": no_candidate}) is None
    assert (
        phase.validate({"target": "failed", "discovery": _discovery_envelope()}) is None
    )

    # Both branches at once violates the exactly-one semantic check.
    both = _discovery_envelope()
    both["noCandidateReason"] = "Also nothing worth pursuing."
    assert any("exactly one" in error for error in discovery_payload_errors(both))
    assert phase.validate({"target": "model_creation", "discovery": both}) is not None

    # Neither branch is reported by both the structural anyOf and the
    # semantic check.
    neither = _discovery_envelope()
    del neither["sketchReference"]
    errors = discovery_payload_errors(neither)
    assert any("exactly one" in error for error in errors)
    assert any("noCandidateReason" in error for error in errors)


def test_discovery_no_candidate_folds_to_completed() -> None:
    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-no-candidate",
        phase=HypothesisDevelopmentPhase.DISCOVERY,
        intent_epoch=0,
    )
    transition = StoreTransition(
        sequence=1,
        from_phase="discovery",
        to_phase="completed",
        occurred_at=_moment(),
        intent_epoch=1,
        reason="discovery found nothing to pursue",
        completion={
            "target": "completed",
            "discovery": _no_candidate_discovery_envelope(),
        },
    )

    folded = fold_transition(state, transition)

    assert folded.phase is HypothesisDevelopmentPhase.COMPLETED
    assert folded.discovery is not None
    assert folded.discovery.sketch_reference is None
    assert folded.discovery.no_candidate_reason is not None
    assert workflow_piping()[("discovery", "completed")] is identity_pipe


def test_discovery_draft_no_candidate_round_trips_and_enforces_exactly_one() -> None:
    draft = DiscoveryDraft.from_dict(_no_candidate_discovery_envelope())
    assert draft.sketch_reference is None
    assert DiscoveryDraft.from_dict(draft.as_dict()) == draft

    with pytest.raises(WorkflowError, match="exactly one"):
        DiscoveryDraft(
            sketch_reference=None,
            no_candidate_reason=None,
            attempts=draft.attempts,
            untouched_data=draft.untouched_data,
            limitations=draft.limitations,
        )
    with pytest.raises(WorkflowError, match="exactly one"):
        DiscoveryDraft(
            sketch_reference=DiscoveryDraft.from_dict(
                _discovery_envelope()
            ).sketch_reference,
            no_candidate_reason="Also nothing.",
            attempts=draft.attempts,
            untouched_data=draft.untouched_data,
            limitations=draft.limitations,
        )
