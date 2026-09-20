from __future__ import annotations

import pytest

from thesis.workflows.registry.hypothesis_development import (
    HYPOTHESIS_DEVELOPMENT_PHASES,
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    HypothesisDevelopmentPhase,
    InvalidPhaseTransition,
    transition_phase,
)


def test_phase_transition_table_is_explicit_and_auditable() -> None:
    assert (
        transition_phase(
            HypothesisDevelopmentPhase.HYPOTHESIS_CREATION,
            HypothesisDevelopmentPhase.FALSIFICATION_TRIALS,
        )
        is HypothesisDevelopmentPhase.FALSIFICATION_TRIALS
    )
    assert (
        transition_phase(
            HypothesisDevelopmentPhase.FALSIFICATION_TRIALS,
            HypothesisDevelopmentPhase.VERDICT,
        )
        is HypothesisDevelopmentPhase.VERDICT
    )
    assert (
        transition_phase(
            HypothesisDevelopmentPhase.STRATEGY_BACKTESTING,
            HypothesisDevelopmentPhase.STRATEGY_RCA,
        )
        is HypothesisDevelopmentPhase.STRATEGY_RCA
    )
    assert (
        transition_phase(
            HypothesisDevelopmentPhase.STRATEGY_RCA,
            HypothesisDevelopmentPhase.STRATEGY_DESIGN,
        )
        is HypothesisDevelopmentPhase.STRATEGY_DESIGN
    )
    with pytest.raises(InvalidPhaseTransition, match="hypothesis_creation to verdict"):
        transition_phase(
            HypothesisDevelopmentPhase.HYPOTHESIS_CREATION,
            HypothesisDevelopmentPhase.VERDICT,
        )
    with pytest.raises(
        InvalidPhaseTransition, match="completed to hypothesis_creation"
    ):
        transition_phase(
            HypothesisDevelopmentPhase.COMPLETED,
            HypothesisDevelopmentPhase.HYPOTHESIS_CREATION,
        )
    assert HYPOTHESIS_DEVELOPMENT_WORKFLOW.phases == HYPOTHESIS_DEVELOPMENT_PHASES


def test_registered_hypothesis_spec_declares_real_phases_and_piping() -> None:
    from thesis.workflows.core.phase import (
        ConversationPhase,
        DeterministicPhase,
        OneShotPhase,
    )
    from thesis.workflows.registry import build_hypothesis_development_spec
    from thesis.workflows.registry.hypothesis_development.phases import (
        HypothesisDevelopmentPhase,
    )
    from thesis.workflows.registry.hypothesis_development.phases.trials import (
        run_falsification_trials,
    )
    from thesis.workflows.registry.hypothesis_development.piping import (
        reset_backtests,
        reset_premise,
    )

    spec = build_hypothesis_development_spec()

    # The conversational phases accept operator input from birth; their agents
    # are prompted through the declared seeds.
    creation = HypothesisDevelopmentPhase.HYPOTHESIS_CREATION.value
    verdict = HypothesisDevelopmentPhase.VERDICT.value
    for phase_value in (creation, verdict):
        declared = spec.phase_specs[phase_value]
        assert isinstance(declared, ConversationPhase)
        assert declared.seed is not None
    # The one-shot agent phases run to a terminal answer through their seeds.
    for phase_value in (
        HypothesisDevelopmentPhase.STRATEGY_DESIGN.value,
        HypothesisDevelopmentPhase.STRATEGY_BACKTESTING.value,
        HypothesisDevelopmentPhase.STRATEGY_RCA.value,
        HypothesisDevelopmentPhase.OPTIMIZATION.value,
    ):
        declared = spec.phase_specs[phase_value]
        assert isinstance(declared, OneShotPhase)
        assert declared.seed is not None

    # The trials phase runs deterministically from the accepted hypothesis
    # contract instead of through an agent.
    trials = HypothesisDevelopmentPhase.FALSIFICATION_TRIALS.value
    trials_spec = spec.phase_specs[trials]
    assert isinstance(trials_spec, DeterministicPhase)
    assert trials_spec.contract is run_falsification_trials

    assert spec.piping
    assert all(callable(pipe) for pipe in spec.piping.values())
    assert spec.piping[("verdict", "hypothesis_creation")] is reset_premise
    assert spec.piping[("strategy_rca", "strategy_design")] is reset_backtests
    transition_keys = {
        (source, target)
        for source, targets in spec.transitions.items()
        for target in targets
    }
    assert set(spec.piping) == transition_keys
