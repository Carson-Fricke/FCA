"""The verdict contract: advertised schema, typed decode, and rejection."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from thesis.agent.runtime.schema import validate_arguments
from thesis.workflows.registry.hypothesis_development.contracts.trials import (
    FalsificationTrial,
)
from thesis.workflows.registry.hypothesis_development.contracts.validation import (
    VERDICT_PAYLOAD_SCHEMA,
    verdict_payload_errors,
)
from thesis.workflows.registry.hypothesis_development.contracts.verdict import (
    FalsificationVerdict,
)
from thesis.workflows.registry.hypothesis_development.errors import WorkflowError
from thesis.workflows.registry.hypothesis_development.spec import (
    build_hypothesis_development_workflow,
)

from ._support import _sketch_reference


def _trial_payload() -> dict[str, object]:
    return {
        "testId": "T01",
        "falsifierId": "f1",
        "jobId": "job-1",
        "digest": "a" * 64,
        "runState": "succeeded",
        "observed": 1.0,
        "falsified": False,
        "detail": None,
    }


def _verdict_payload() -> dict[str, object]:
    return {
        "sketchReference": _sketch_reference(),
        "assumptionAssessments": [
            {
                "assumptionId": "continuation",
                "status": "supported",
                "reason": "Trial survived",
                "testIds": ["T01"],
            }
        ],
        "outcome": "supported",
        "reason": "the planned falsifiers survived the trials",
        "evidence": "sharpe stayed above zero.",
        "trials": [_trial_payload()],
    }


def _phase():
    return build_hypothesis_development_workflow().spec.phase_specs["verdict"]


def test_verdict_schema_and_validator_accept_complete_payloads() -> None:
    phase = _phase()
    completion = {"target": "strategy_design", "verdict": _verdict_payload()}
    assert phase.validate is not None
    assert verdict_payload_errors(_verdict_payload()) == []
    assert phase.validate(completion) is None
    assert not validate_arguments(phase.completion_schema, completion)


def test_verdict_completion_schema_advertises_declared_targets() -> None:
    schema = _phase().completion_schema
    assert isinstance(schema, dict)
    assert schema["required"] == ["verdict"]
    assert schema["properties"]["verdict"] is VERDICT_PAYLOAD_SCHEMA
    assert schema["properties"]["target"]["enum"] == [
        "strategy_design",
        "model_creation",
        "discovery",
    ]


def _bad_outcome(payload: dict[str, object]) -> None:
    payload["outcome"] = "maybe"


def _missing_reason(payload: dict[str, object]) -> None:
    del payload["reason"]


def _scalar_trials(payload: dict[str, object]) -> None:
    payload["trials"] = "T01"


def _incomplete_trial(payload: dict[str, object]) -> None:
    payload["trials"] = [{"testId": "T01"}]


def _wrong_evidence(payload: dict[str, object]) -> None:
    payload["evidence"] = 7


@pytest.mark.parametrize(
    "mutate",
    [_bad_outcome, _missing_reason, _scalar_trials, _incomplete_trial, _wrong_evidence],
)
def test_verdict_validator_rejects_malformed_payloads(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    payload = _verdict_payload()
    mutate(payload)
    phase = _phase()
    failure = phase.validate({"target": "strategy_design", "verdict": payload})
    assert failure is not None
    assert "verdict" in failure


def test_falsification_verdict_decodes_typed_trials_and_round_trips() -> None:
    verdict = FalsificationVerdict.from_dict(_verdict_payload())
    assert verdict.outcome == "supported"
    assert verdict.evidence == "sharpe stayed above zero."
    assert isinstance(verdict.trials[0], FalsificationTrial)
    assert FalsificationVerdict.from_dict(verdict.as_dict()) == verdict


def test_falsification_verdict_rejects_bad_outcome_and_trials() -> None:
    with pytest.raises(WorkflowError, match="outcome"):
        FalsificationVerdict.from_dict({"outcome": "maybe", "reason": "no"})
    with pytest.raises(WorkflowError, match="trials"):
        FalsificationVerdict.from_dict(
            {"outcome": "supported", "reason": "yes", "trials": "T01"}
        )
    with pytest.raises(WorkflowError, match="reason"):
        FalsificationVerdict.from_dict({"outcome": "supported"})
