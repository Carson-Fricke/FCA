"""Falsification-trials prompt and its protocol."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from thesis.agent.router import InvocationResult

from ..contracts import (
    FalsificationPlanDraft,
)
from ..primitives import _format_observed
from .creation import _LATEX_INSTRUCTION

if TYPE_CHECKING:
    from ..state import HypothesisDevelopmentState


class FalsificationPlanner(Protocol):
    """Injected boundary that turns model output into a falsification plan."""

    def compose(self, invocation: InvocationResult) -> FalsificationPlanDraft: ...


def _falsification_turn_prompt(
    state: HypothesisDevelopmentState,
    metrics: Mapping[str, object],
) -> str:
    """Task prompt asking the falsification sub-agent to author its empirical analysis."""
    del metrics
    claim = state.hypothesis.front_matter.claim if state.hypothesis else ""
    disagreement = (
        state.hypothesis.front_matter.disagreement if state.hypothesis else ""
    )
    universe = (
        ", ".join(state.hypothesis.front_matter.universe) if state.hypothesis else ""
    )
    horizon = state.hypothesis.front_matter.horizon if state.hypothesis else ""

    lines = [
        "You are the Thesis research falsification trials orchestrator sub-agent.",
        "The empirical falsification evaluation has executed against historical data.",
        "You are the agent that evaluates these trials: there is no separate "
        "diagnosis step. Read the typed results below, including any run state or "
        "failure message, and explain in prose exactly what survived, what was "
        "refuted, and what went wrong.",
        "If a trial produced no observation because the harness or the data failed, "
        "say so plainly, name the observable and the failure text, and state whether "
        "the operator's direction is needed before the verdict can be trusted. Do not "
        "invent observations, and do not treat a failed run as a refutation.",
        "",
        f"Hypothesis Claim: {claim}",
        f"Null / Disagreement: {disagreement}",
        f"Universe: {universe} | Horizon: {horizon}",
        "",
        "Empirical Trial Observables & Results:",
    ]
    tests_by_id = {t.test_id: t for t in (state.plan.tests if state.plan else ())}
    for t in state.trials:
        planned = tests_by_id.get(t.test_id)
        cond = planned.condition if planned else ""
        thresh = _format_observed(planned.threshold if planned else 0.0)
        obs = _format_observed(t.observed)
        if t.observed is None:
            status = f"NO OBSERVATION (run state {t.run_state})"
        elif t.falsified:
            status = "REFUTED"
        else:
            status = "SURVIVED (not refuted)"
        desc = (
            planned.description if planned and planned.description else t.falsifier_id
        )
        lines.append(
            f"- Test {t.test_id} ({desc}): Observed {obs} vs threshold {cond} {thresh} -> {status}"
        )
        if t.detail:
            lines.append(f"  Failure detail: {t.detail}")

    if state.plan and state.plan.controls:
        lines.append("")
        lines.append("Negative & Placebo Controls:")
        for c in state.plan.controls:
            lines.append(
                f"- {c.control_id}: {c.description} (Expected: {c.expectation})"
            )

    lines.extend(
        [
            "",
            "Format your response in clean Markdown with appropriate headers and bullet points.",
            _LATEX_INSTRUCTION,
            "Lead directly with your empirical takeaways; do not output raw JSON or code blocks.",
        ]
    )
    return "\n".join(lines)
