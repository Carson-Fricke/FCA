"""The discovery phase: capabilities, contract, pipes, folds, and legacy runs."""

from __future__ import annotations

from datetime import UTC, datetime

from thesis.agent.runtime.schema import validate_arguments
from thesis.workflows.core.execution_store import StoreTransition
from thesis.workflows.registry.hypothesis_development.contracts.discovery import (
    DiscoveryDraft,
)
from thesis.workflows.registry.hypothesis_development.contracts.validation import (
    DISCOVERY_COMPLETION_SCHEMA,
    discovery_payload_errors,
)
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

from ._support import WORKSPACE, _discovery_envelope, _hypothesis_envelope


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
    del missing_candidate["candidate"]
    failure = phase.validate(
        {"target": "model_creation", "discovery": missing_candidate}
    )
    assert failure is not None
    assert "discovery.candidate" in failure
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
    assert (
        folded.discovery.candidate.opportunity
        == "Five-day momentum in SPY continues over the next five days."
    )
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
            completion={"trials": []},
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


def test_retired_hypothesis_creation_decodes_to_model_creation() -> None:
    assert (
        phase_from_value("hypothesis_creation")
        is HypothesisDevelopmentPhase.MODEL_CREATION
    )

    legacy = StoreTransition(
        sequence=1,
        from_phase="hypothesis_creation",
        to_phase="falsification_trials",
        occurred_at=_moment(),
        intent_epoch=1,
        reason="legacy creation contract accepted",
        completion={
            "target": "falsification_trials",
            "hypothesis": _hypothesis_envelope(),
        },
    )
    rebuilt = rebuild_state(WORKSPACE, "execution-legacy", [legacy])

    assert rebuilt.phase is HypothesisDevelopmentPhase.FALSIFICATION_TRIALS
    assert (
        rebuilt.transitions[0].from_phase is HypothesisDevelopmentPhase.MODEL_CREATION
    )
    assert rebuilt.draft is not None
    assert rebuilt.draft.claim == _hypothesis_envelope()["claim"]


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
    candidate = draft.as_dict()["candidate"]
    assert isinstance(candidate, dict)
    assert candidate["informationTime"]

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
