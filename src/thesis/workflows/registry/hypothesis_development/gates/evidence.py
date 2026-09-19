"""Evidence gate binding planned falsifiers to recorded trials."""

from __future__ import annotations

from collections.abc import Sequence

from ..contracts import (
    FalsificationPlan,
    FalsificationTrial,
)
from ..primitives import _DIGEST


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
