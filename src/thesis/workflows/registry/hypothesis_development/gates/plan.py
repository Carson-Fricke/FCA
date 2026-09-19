"""Falsification-plan gate binding tests to declared falsifiers."""

from __future__ import annotations

from thesis.research.hypothesis import (
    HypothesisFrontMatter,
)

from ..contracts import (
    FalsificationPlan,
)


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
