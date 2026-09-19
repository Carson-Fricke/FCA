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


def test_registered_hypothesis_spec_uses_real_phase_executors_and_piping() -> None:
    from thesis.workflows.registry import build_hypothesis_development_spec
    from thesis.workflows.registry.hypothesis_development.phases import (
        HypothesisDevelopmentPhase,
    )
    from thesis.workflows.registry.hypothesis_development.phases.backtesting import (
        run_strategy_backtest,
    )
    from thesis.workflows.registry.hypothesis_development.phases.creation import (
        run_creation_turn,
    )
    from thesis.workflows.registry.hypothesis_development.phases.design import (
        advance_design,
    )
    from thesis.workflows.registry.hypothesis_development.phases.optimization import (
        optimize,
    )
    from thesis.workflows.registry.hypothesis_development.phases.strategy_rca import (
        diagnose_strategy,
    )
    from thesis.workflows.registry.hypothesis_development.phases.trials import (
        advance_trials,
    )
    from thesis.workflows.registry.hypothesis_development.phases.verdict import (
        advance_verdict,
    )
    from thesis.workflows.registry.hypothesis_development.piping import (
        reset_backtests,
        reset_premise,
    )

    spec = build_hypothesis_development_spec()

    executors = spec.executors
    assert (
        executors[HypothesisDevelopmentPhase.HYPOTHESIS_CREATION.value]
        is run_creation_turn
    )
    assert executors[HypothesisDevelopmentPhase.FALSIFICATION_TRIALS.value] is (
        advance_trials
    )
    assert executors[HypothesisDevelopmentPhase.VERDICT.value] is advance_verdict
    assert executors[HypothesisDevelopmentPhase.STRATEGY_DESIGN.value] is advance_design
    assert executors[HypothesisDevelopmentPhase.STRATEGY_BACKTESTING.value] is (
        run_strategy_backtest
    )
    assert executors[HypothesisDevelopmentPhase.STRATEGY_RCA.value] is diagnose_strategy
    assert executors[HypothesisDevelopmentPhase.OPTIMIZATION.value] is optimize
    active_phases = (
        HypothesisDevelopmentPhase.HYPOTHESIS_CREATION.value,
        HypothesisDevelopmentPhase.FALSIFICATION_TRIALS.value,
        HypothesisDevelopmentPhase.VERDICT.value,
        HypothesisDevelopmentPhase.STRATEGY_DESIGN.value,
        HypothesisDevelopmentPhase.STRATEGY_BACKTESTING.value,
        HypothesisDevelopmentPhase.STRATEGY_RCA.value,
        HypothesisDevelopmentPhase.OPTIMIZATION.value,
    )
    assert len({executors[phase] for phase in active_phases}) == len(active_phases)

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
