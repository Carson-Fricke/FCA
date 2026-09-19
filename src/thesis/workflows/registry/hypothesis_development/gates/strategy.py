"""Verdict-advance and strategy-design acceptance gates."""

from __future__ import annotations

from collections.abc import Sequence

from thesis.research.hypothesis import (
    HypothesisFalsifier,
    HypothesisFrontMatter,
)
from thesis.strategies.bundles import StrategyBundle

from ..contracts import (
    ExitCriterion,
    FalsificationVerdict,
)
from ..primitives import VERDICT_REFUTED


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
