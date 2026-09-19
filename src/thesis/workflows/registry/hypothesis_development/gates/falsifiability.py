"""Falsifiability gate for a hypothesis's front matter."""

from __future__ import annotations

from thesis.research.hypothesis import (
    FALSIFIER_CONDITIONS,
    HypothesisFrontMatter,
)


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
