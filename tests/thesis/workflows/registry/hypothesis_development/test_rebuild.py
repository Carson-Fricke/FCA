"""The store-side rebuild fold: transitions-with-completions into typed state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from thesis.workflows.core.execution_store import StoreTransition
from thesis.workflows.registry.hypothesis_development.errors import WorkflowError
from thesis.workflows.registry.hypothesis_development.rebuild import (
    fold_transition,
    rebuild_state,
)
from thesis.workflows.registry.hypothesis_development.state import (
    HypothesisDevelopmentState,
)

from ._support import WORKSPACE, _hypothesis_envelope, _model

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

EXECUTION = "execution-1"


def _trial_payload(
    test_id: str = "t1", *, observed: float | None = 1.0, falsified: bool = False
) -> dict[str, object]:
    from thesis.workflows.registry.hypothesis_development import FalsificationTrial

    return FalsificationTrial(
        test_id=test_id,
        falsifier_id="f1",
        run_id="job-1",
        digest="a" * 64,
        run_state="completed",
        observed=observed,
        falsified=falsified,
        detail=None,
    ).as_dict()


def _transition(
    sequence: int,
    from_phase: str,
    to_phase: str,
    completion: dict[str, object] | None = None,
) -> StoreTransition:
    return StoreTransition(
        sequence=sequence,
        from_phase=from_phase,
        to_phase=to_phase,
        occurred_at=NOW + timedelta(minutes=sequence),
        intent_epoch=0,
        reason="accepted contract",
        completion=completion or {},
    )


def _empty_state() -> HypothesisDevelopmentState:
    return HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id=EXECUTION,
        phase=_phase("hypothesis_creation"),
        intent_epoch=0,
    )


def _phase(value: str) -> Any:
    from thesis.workflows.registry.hypothesis_development import (
        HypothesisDevelopmentPhase,
    )

    return HypothesisDevelopmentPhase(value)


def test_full_run_folds_step_by_step_into_the_typed_state() -> None:
    # Step 1: the accepted hypothesis contract becomes the draft.
    state = fold_transition(
        _empty_state(),
        _transition(
            1,
            "hypothesis_creation",
            "falsification_trials",
            {"hypothesis": _hypothesis_envelope(), "target": "falsification_trials"},
        ),
    )
    assert state.phase is _phase("falsification_trials")
    assert state.draft is not None
    assert state.draft.claim == _hypothesis_envelope()["claim"]
    assert state.draft.model.model_id == _model()["modelId"]
    assert [falsifier.falsifier_id for falsifier in state.draft.falsifiers] == ["f1"]
    # The fold is store-only: the hypothesis document side-store is not read.
    assert state.hypothesis is None

    # Step 2: the trials phase's completion becomes the recorded trials.
    trials = [
        _trial_payload("t1", observed=1.2, falsified=False),
        _trial_payload("t2", observed=None, falsified=False),
    ]
    state = fold_transition(
        state,
        _transition(
            2,
            "falsification_trials",
            "verdict",
            {"trials": trials, "target": "verdict"},
        ),
    )
    assert state.phase is _phase("verdict")
    assert [trial.test_id for trial in state.trials] == ["t1", "t2"]
    assert state.trials[0].observed == 1.2
    assert state.trials[1].observed is None
    # The draft carries forward across the trials move.
    assert state.draft is not None

    # Step 3: the verdict contract becomes the typed verdict.
    state = fold_transition(
        state,
        _transition(
            3,
            "verdict",
            "strategy_design",
            {
                "verdict": {
                    "outcome": "supported",
                    "reason": "every planned falsifier survived",
                    "trials": trials,
                },
                "target": "strategy_design",
            },
        ),
    )
    assert state.phase is _phase("strategy_design")
    assert state.verdict is not None
    assert state.verdict.outcome == "supported"
    assert state.verdict.falsified is False
    assert [trial.test_id for trial in state.verdict.trials] == ["t1", "t2"]

    # Step 4: the strategy chain's structural moves carry state unchanged.
    state = fold_transition(
        state,
        _transition(4, "strategy_design", "strategy_backtesting"),
    )
    assert state.phase is _phase("strategy_backtesting")
    assert state.verdict is not None and state.draft is not None


def test_strategy_rca_redesign_clears_the_previous_backtests() -> None:
    from dataclasses import replace

    from thesis.workflows.backtests import BacktestRunResult
    from thesis.workflows.registry.hypothesis_development import (
        HypothesisDevelopmentPhase,
    )

    state = rebuild_state(WORKSPACE, EXECUTION, [])
    state = replace(
        state,
        strategy_backtest=BacktestRunResult(
            job_id="job-1",
            state="completed",
            mode="local",
            manifest={"manifestId": "m", "sha256": "a" * 64},
            metrics={"sharpe": -1.0},
            result_artifacts=(),
        ),
    )
    state = replace(state, phase=HypothesisDevelopmentPhase.STRATEGY_BACKTESTING)

    state = fold_transition(
        state,
        _transition(1, "strategy_backtesting", "strategy_rca"),
    )
    assert state.strategy_backtest is not None
    state = fold_transition(
        state,
        _transition(2, "strategy_rca", "strategy_design"),
    )
    assert state.phase is HypothesisDevelopmentPhase.STRATEGY_DESIGN
    assert state.strategy_backtest is None
    assert state.optimization_backtest is None


def test_circle_back_resets_to_a_fresh_creation_state() -> None:
    trials = [_trial_payload("t1", observed=-0.5, falsified=True)]
    state = rebuild_state(
        WORKSPACE,
        EXECUTION,
        [
            _transition(
                1,
                "hypothesis_creation",
                "falsification_trials",
                {"hypothesis": _hypothesis_envelope()},
            ),
            _transition(2, "falsification_trials", "verdict", {"trials": trials}),
        ],
    )

    state = fold_transition(
        state,
        _transition(
            3,
            "verdict",
            "hypothesis_creation",
            {
                "verdict": {
                    "outcome": "refuted",
                    "reason": "the planned falsifier fired",
                    "trials": trials,
                },
                "target": "hypothesis_creation",
            },
        ),
    )

    assert state.phase is _phase("hypothesis_creation")
    assert state.hypothesis_cycles == 0
    # The refuting verdict survives the reset; the refuted premise's derived
    # artifacts belong to the previous attempt.
    assert state.verdict is not None
    assert state.verdict.falsified is True
    assert state.draft is None
    assert state.hypothesis is None
    assert state.trials == ()
    assert state.plan is None
    assert state.research_model_reference is None
    assert state.strategy_bundle is None
    assert state.exit_criteria == ()
    assert state.strategy_backtest is None
    assert state.optimization_backtest is None
    assert [item.sequence for item in state.transitions] == [1, 2, 3]


def test_rebuild_state_of_the_whole_sequence_equals_the_stepwise_fold() -> None:
    transitions = [
        _transition(
            1,
            "hypothesis_creation",
            "falsification_trials",
            {"hypothesis": _hypothesis_envelope()},
        ),
        _transition(
            2, "falsification_trials", "verdict", {"trials": [_trial_payload()]}
        ),
    ]

    folded = rebuild_state(WORKSPACE, EXECUTION, transitions)

    assert folded.phase is _phase("verdict")
    assert folded.draft is not None
    assert [trial.test_id for trial in folded.trials] == ["t1"]
    assert folded.transitions[-1].at == NOW + timedelta(minutes=2)
    assert folded.transitions[-1].reason == "accepted contract"


def test_the_fold_rejects_unknown_phases_and_missing_payloads() -> None:
    with pytest.raises(WorkflowError):
        fold_transition(
            _empty_state(),
            _transition(1, "hypothesis_creation", "not-a-phase"),
        )
    with pytest.raises(WorkflowError):
        fold_transition(
            _empty_state(),
            _transition(1, "hypothesis_creation", "falsification_trials", {}),
        )
    with pytest.raises(WorkflowError):
        fold_transition(
            _empty_state(),
            _transition(1, "falsification_trials", "verdict", {"trials": "nope"}),
        )
