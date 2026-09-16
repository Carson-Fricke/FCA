"""Deterministic acceptance gates for each phase.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from thesis.research.hypothesis import (
    FALSIFIER_CONDITIONS,
    HypothesisFalsifier,
    HypothesisFrontMatter,
)
from thesis.research.models import ResearchModel
from thesis.strategies.bundles import StrategyBundle

from .contracts import (
    ExitCriterion,
    FalsificationPlan,
    FalsificationTrial,
    FalsificationVerdict,
)
from .primitives import _DIGEST, VERDICT_REFUTED


def check_falsifiability(front_matter: HypothesisFrontMatter) -> str | None:
    """Return the failure reason when a hypothesis is not falsifiable."""
    if not front_matter.claim.strip():
        return "hypothesis requires a non-empty claim"
    if not front_matter.disagreement.strip():
        return (
            "hypothesis requires a non-empty disagreement: state what the "
            "no-structure null predicts and why the intuition departs from it; "
            "without a departure there is no regularity to trade"
        )
    if not front_matter.falsifiers:
        return "hypothesis requires at least one falsifier"
    for falsifier in front_matter.falsifiers:
        if not falsifier.observable.strip():
            return "every falsifier requires a non-empty observable"
        if falsifier.condition not in FALSIFIER_CONDITIONS:
            return "every falsifier requires a supported condition"
        if not falsifier.test.strip():
            return "every falsifier requires a non-empty test"
    return None


def check_research_model(
    model: ResearchModel, front_matter: HypothesisFrontMatter
) -> str | None:
    """Return the failure reason when Layer A cannot observe its falsifiers."""
    declared_outputs = set(model.outputs)
    missing = sorted(
        falsifier.observable
        for falsifier in front_matter.falsifiers
        if falsifier.observable not in declared_outputs
    )
    if missing:
        return (
            "research model must publish every falsifier observable; missing model "
            f"outputs: {missing}"
        )
    return None


def check_falsification_plan(
    plan: FalsificationPlan, front_matter: HypothesisFrontMatter
) -> str | None:
    """Return the failure reason when a plan cannot falsify the hypothesis."""
    if not plan.tests:
        return "falsification plan requires at least one falsifier test"
    if not plan.controls:
        return "falsification plan requires at least one negative/placebo control"
    declared = {
        falsifier.falsifier_id
        for falsifier in front_matter.falsifiers
        if falsifier.falsifier_id is not None
    }
    for test in plan.tests:
        if test.falsifier_id not in declared:
            return (
                f"falsification test {test.test_id!r} references undeclared "
                f"falsifier {test.falsifier_id!r}"
            )
    return None


def check_evidence_bound(
    plan: FalsificationPlan | None, trials: Sequence[FalsificationTrial]
) -> str | None:
    """Return the failure reason when a planned falsifier lacks evidence."""
    if plan is None:
        return "no falsification plan was recorded"
    by_falsifier = {trial.falsifier_id: trial for trial in trials}
    for test in plan.tests:
        trial = by_falsifier.get(test.falsifier_id)
        if trial is None:
            return f"falsifier {test.falsifier_id!r} has no recorded trial"
        if trial.observed is None:
            return (
                f"falsifier {test.falsifier_id!r} has no numeric observation bound "
                "to a run digest"
            )
        if _DIGEST.fullmatch(trial.digest) is None:
            return f"falsifier {test.falsifier_id!r} has no run digest"
    return None


def check_verdict_advances(verdict: FalsificationVerdict) -> str | None:
    """Return the failure reason when a verdict may not reach strategy design."""
    if verdict.outcome == VERDICT_REFUTED:
        return (
            "the hypothesis was refuted; a refuted premise cannot advance to "
            "strategy design and must return to hypothesis"
        )
    return None


def check_strategy_design(
    bundle: StrategyBundle | None,
    exit_criteria: Sequence[ExitCriterion],
    front_matter: HypothesisFrontMatter,
) -> str | None:
    """Return the failure reason when a strategy is not a falsification tool."""
    if bundle is None:
        return "strategy design requires a validated strategy bundle"
    if not exit_criteria:
        return "strategy design requires declared exit criteria"
    falsifiers = front_matter.falsifiers
    for criterion in exit_criteria:
        if not any(
            _exit_rechecks_falsifier(criterion, falsifier) for falsifier in falsifiers
        ):
            return (
                "every exit criterion must re-check a declared falsifier "
                f"(observable={criterion.observable!r}, "
                f"condition={criterion.condition!r})"
            )
    return None


def _exit_rechecks_falsifier(
    criterion: ExitCriterion, falsifier: HypothesisFalsifier
) -> bool:
    if criterion.falsifier_id is not None:
        return criterion.falsifier_id == falsifier.falsifier_id
    return (
        criterion.observable == falsifier.observable
        and criterion.condition == falsifier.condition
        and criterion.threshold == falsifier.threshold
    )


# -- composers --------------------------------------------------------------
