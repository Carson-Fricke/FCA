"""Research-model gate binding falsifiers to published observables."""

from __future__ import annotations

from thesis.research.hypothesis import (
    HypothesisFrontMatter,
)
from thesis.research.models import ResearchModel


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
