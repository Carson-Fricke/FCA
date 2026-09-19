"""Strategy-design prompt and its protocol."""

from __future__ import annotations

from typing import Protocol

from thesis.agent.router import InvocationResult
from thesis.research.hypothesis import HypothesisDocument

from ..contracts import (
    StrategyDesignDraft,
)


def _hypothesis_context_lines(
    document: HypothesisDocument,
    model_reference: str | None = None,
    fitted_reference: str | None = None,
) -> list[str]:
    """Render the finalized model so later phases work from it, not the claim.

    The declared falsifier ids are listed explicitly: the falsification planner
    must bind every test to one of them, and the strategy author must phrase
    every exit criterion as a re-check of one of them (Layer B).
    """
    front = document.front_matter
    lines = [
        f"Claim: {front.claim}",
        f"Null / disagreement: {front.disagreement}",
        f"Horizon: {front.horizon}",
        f"Universe: {', '.join(front.universe)}",
    ]
    if model_reference is not None:
        lines.append(f"Workspace research model: {model_reference}")
    if fitted_reference is not None:
        lines.append(f"Frozen Layer A fit: {fitted_reference}")
    lines.append("Declared falsifiers:")
    for falsifier in front.falsifiers:
        lines.append(
            f"- {falsifier.falsifier_id or falsifier.observable}: "
            f"{falsifier.observable} {falsifier.condition} "
            f"{falsifier.threshold:g} :: {falsifier.test}"
        )
    if document.body:
        lines.extend(["Model specification:", document.body])
    return lines


class StrategyDesigner(Protocol):
    """Injected boundary that turns model output into a strategy design."""

    def compose(self, invocation: InvocationResult) -> StrategyDesignDraft: ...
