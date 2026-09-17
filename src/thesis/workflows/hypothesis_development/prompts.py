"""Prompt builders and composer protocols.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

from thesis.agent.router import InvocationResult
from thesis.research.hypothesis import HypothesisDocument

from .contracts import (
    FalsificationPlanDraft,
    HypothesisDraft,
    RcaDiagnosis,
    StrategyDesignDraft,
)
from .primitives import _CREATION_TURN_SCHEMA, _format_observed

if TYPE_CHECKING:
    from .state import HypothesisDevelopmentState


class HypothesisComposer(Protocol):
    """Injected boundary that turns model output into a hypothesis draft."""

    def compose(self, invocation: InvocationResult) -> HypothesisDraft: ...


class FalsificationPlanner(Protocol):
    """Injected boundary that turns model output into a falsification plan."""

    def compose(self, invocation: InvocationResult) -> FalsificationPlanDraft: ...


class RcaComposer(Protocol):
    """Injected boundary that turns model output into an RCA diagnosis."""

    def compose(self, invocation: InvocationResult) -> RcaDiagnosis: ...


def _event_output_text(payload: Mapping[str, object]) -> str | None:
    """Return the visible text carried by one sub-agent output payload."""
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    artifact = payload.get("artifact")
    if isinstance(artifact, Mapping):
        content = artifact.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def _conversation_lines(
    ideas: str, conversation: Sequence[tuple[str, str]]
) -> list[str]:
    lines = [f"Operator's original idea:\n{ideas}", "", "Conversation so far:"]
    for role, text in conversation:
        lines.append(f"{role}: {text}")
    return lines


#: Shared framing for both hypothesis-creation prompts: the phase builds a
#: statistical model of the operator's intuition and tests it on history.
_CREATION_MISSION = (
    "You are the hypothesis-creation agent of a falsification-first research "
    "pipeline. The operator brings an intuition about a trading opportunity. "
    "Your job is to turn that intuition into ONE statistical model of the "
    "assets involved and to decide, from HISTORICAL data, whether a regularity "
    "matching the intuition actually exists. You are not designing, sizing, or "
    "costing a trade yet, and you never propose a forward-looking experiment: "
    "the model is estimated and falsified on history that is available today."
)


#: Every operator-facing surface renders KaTeX, so math must be authored as
#: LaTeX rather than plain text or fenced code. The `\(...\)` and `\[...\]`
#: delimiters are used instead of `$` because a bare `$` collides with currency
#: and dollar-denominated numbers.
_LATEX_INSTRUCTION = (
    "Render every mathematical expression as LaTeX for KaTeX. Write inline math "
    "as \\(...\\) and display equations as \\[...\\] on their own line. Use "
    "LaTeX for all symbols, operators, subscripts, fractions, Greek letters and "
    "statistics, such as \\(\\beta\\), \\(\\sigma\\), \\(\\rho\\), \\(R^2\\), "
    "\\(\\alpha\\) and \\(\\frac{a}{b}\\). Never wrap plain numbers or "
    "currency amounts in math delimiters, never use a bare $ for math, and "
    "never place math inside a fenced code block."
)


#: The two falsification layers the model serves.
_CREATION_LAYERS = (
    "Layer A (this pipeline, on history): the model cannot be fit, the fitted "
    "model shows no regularity that beats a no-structure null, or the "
    "regularity exists but points the wrong way or is far weaker than the "
    "intuition claims. Any of these refutes the hypothesis, which then gets "
    "adjusted and tried again.",
    "Markets are noisy, so Layer A asks whether any stable regularity survives "
    "reasonable attempts to falsify it; it does not demand strategy-grade "
    "performance. Use uncertainty-aware, conservative rejection thresholds. A "
    "modest regularity that has the predicted sign, is distinguishable from the "
    "no-structure null, and survives controls is a successful research result. "
    "Do not refute it merely because it misses an ambitious effect size, R-squared, "
    "hit rate, Sharpe, or conventional significance cutoff. Treat borderline "
    "evidence as inconclusive, not refuted. Economic strength, costs, and trade "
    "quality are evaluated later in strategy design.",
    "Layer B (later, while a position is open): the same statistics are "
    "recomputed on a trailing window; if they leave the range the model showed "
    "historically, the idea behind the entry has been refuted and the position "
    "exits. The model you build here is the instrument for both layers.",
)


#: Model families the agent may reach for; the list is illustrative, not a
#: menu the operator must choose from.
_CREATION_MODEL_FAMILIES = (
    "- Two or more assets ('A tracks B but underperforms it', 'A and B look "
    "cointegrated, can we trade mean reversion?'): regress A's returns on B's "
    "(alpha, beta, R^2) over the full sample and rolling windows; the "
    "log-price spread A - beta*B with a stationarity/cointegration test and a "
    "mean-reversion half-life; the drift and hit rate of the relative return.",
    "- Structure of one asset's price action ('bitcoin is trendy', 'A cycles "
    "between peaks and troughs'): return autocorrelation at several lags, "
    "variance-ratio tests, a Hurst exponent, the hit rate of a simple "
    "trend-following rule; for periodicity, the periodogram or spectral peak "
    "of the detrended series and its autocorrelation at the claimed period, "
    "against a random-walk baseline.",
    "- Options and volatility ('vol is rich/cheap when ...'): implied minus "
    "subsequently realized volatility conditional on the regime, the variance "
    "risk premium, skew or term-structure slope versus realized outcomes, "
    "delta-hedged option PnL as the estimator.",
)


_CREATION_TURN_INSTRUCTIONS = (
    "Work like this:",
    "1. Restate the intuition as the regularity it implies, in statistical "
    "terms, and decide which assets belong in the model, adding a benchmark or "
    "control series when one is natural. Infer the assets from the idea; ask "
    "only when a required asset is genuinely ambiguous, and propose a default "
    "even then.",
    "2. Propose the model yourself. Choose the estimator, the sampling "
    "frequency, the lookback/rolling windows and the sample period, and give a "
    "one-clause reason for each choice. Do not ask the operator which "
    "statistics, tests, windows or thresholds to use: that is your job. Be "
    "concrete and creative; if several models fit, name the primary one and "
    "the alternative. Model families you may reach for:",
    *_CREATION_MODEL_FAMILIES,
    "3. State what the intuition PREDICTS the estimates will look like "
    "(direction and rough magnitude) and what a no-structure null "
    "(independence, random walk, fairly priced volatility) would produce "
    "instead.",
    "4. Define the Layer A falsifiers: numeric conditions on the estimated "
    "model that, if observed in history, refute the intuition. Each names an "
    "observable (a statistic the model produces), a comparison (lt, lte, gt or "
    "gte), a threshold and the exact computation. Cover the failure modes: the "
    "model cannot be fit or the sample is too short; the fit does not beat the "
    "null; the regularity points the wrong way or is too weak. Add at least "
    "one negative control (shuffled or permuted returns, an unrelated asset "
    "pair, a placebo period) that should show no regularity. Calibrate thresholds "
    "to sampling noise with confidence intervals, bootstrap/permutation baselines, "
    "or similarly robust uncertainty estimates. Falsify only when evidence supports "
    "no stable regularity or the wrong direction; do not use tight aspirational "
    "trading-performance thresholds as Layer A falsifiers.",
    "5. Say which observables double as Layer B monitors and on what trailing "
    "window. Sizing, costs, borrow, financing, execution and portfolio "
    "construction belong to strategy design; do not ask about them now.",
    _LATEX_INSTRUCTION,
    "Return a single JSON object with exactly {message, advance}. The message "
    "is concise operator-facing prose: lead with the proposed model, not with "
    "questions. Format the message in clear Markdown with bullet-point lists "
    "under three distinct sections: '### Model' (regularity predicted vs null, "
    "estimator specification, equations), '### Data' (assets/universe, sampling "
    "frequency, sample window, minimum session counts), and '### Falsifiers & "
    "Controls' (Layer A falsifiers list with observable/threshold, negative controls, "
    "and Layer B live monitors). The boolean advance is your judgment: set it true "
    "only when the model, expected estimates, at least one falsifier, and at least one "
    "control are sufficiently specified for operator review; set it false when "
    "another research turn is needed. On the first agent turn this value reports "
    "readiness only and the workflow always waits for operator input. On later "
    "turns, judge whether the operator's latest prose confirms the proposal: set "
    "advance true for clear acceptance in any wording, or false when the operator "
    "provides feedback, asks a question, or remains ambiguous. When advance is true "
    "because the operator has accepted the proposal, do not restate or re-summarize "
    "the model: reply with one short sentence acknowledging the acceptance and "
    "confirming you are finalizing the same model. The exact response schema is:",
    _CREATION_TURN_SCHEMA,
)


_CREATION_FINALIZE_INSTRUCTIONS = (
    "Return one JSON object with these fields:",
    "- claim: one single-line sentence stating the regularity the intuition "
    "predicts, in model terms.",
    "- disagreement: one single-line sentence stating what the no-structure "
    "null predicts instead and why the intuition departs from it. Without a "
    "departure there is no regularity to trade.",
    "- riskFactor: the main reason the regularity could be spurious or regime-bound.",
    "- horizon: the sampling frequency and the estimation window(s) of the model.",
    "- universe: the ticker symbols in the model, including any benchmark or "
    "control series.",
    "- falsifiers: the Layer A falsifiers. Each has falsifierId, observable (a "
    "snake_case identifier for the statistic the model produces), condition "
    "(lt, lte, gt or gte), threshold (a number) and test (the exact "
    "computation: data, frequency, sample period, method). A falsifier fires "
    "when the observed value satisfies condition/threshold, and a fired "
    "falsifier refutes the claim. Include at least one that catches 'the model "
    "cannot be fit or shows no regularity' (e.g. data_ok < 1.0 or sample_valid < 1.0) and one that catches 'the "
    "regularity points the wrong way or is too weak'. State in the test text "
    "which falsifiers double as Layer B monitors and on what trailing window.",
    "- body: Markdown with the full model specification: variables and "
    "transformations, the estimator, the expected estimates versus the null, "
    "the negative controls, the Layer B monitoring rule and known limitations. "
    + _LATEX_INSTRUCTION,
    "- model (required): a statistical research model, NOT a strategy. Include "
    "modelId, semantic version, description, source defining "
    "fit(context) -> dict[str, number] and observe(fit, context) -> "
    "dict[str, number], fitEntrypoint='fit', observeEntrypoint='observe', "
    "outputs "
    "listing every falsifier observable, tests, and harnessConfiguration with "
    "symbols listing every required symbol. The plain context contains quotes, "
    "trades, trade_prices and returns by symbol; it exposes no orders, positions or "
    "sizing. The model source may import only math, statistics and numpy. If "
    "sample data is missing, unidentifiable, or inadequate, fit and observe must "
    "return explicit numeric failure indicators (such as data_ok=0.0 or sample_valid=0.0) "
    "rather than crashing with unhandled exceptions, so that falsifiers can cleanly observe "
    "and record the empirical failure.",
    "- model.indicators (optional): explicitly scoped reusable inputs. A global "
    "binding names a built-in indicator (scope, name, alias, symbol, parameters). "
    "A workspace binding additionally carries either contentSha256 for an "
    "already-published workspace indicator or a definition with name, version, "
    "description, source defining calculate(context, parameters), entrypoint, "
    "parameterSchema and tests. Never encode an indicator as a strategy.",
    "Omit sizing; it belongs to strategy design. Do not include operator-facing prose.",
)


def _creation_turn_prompt(
    ideas: str, conversation: Sequence[tuple[str, str]], *, first_turn: bool
) -> str:
    lines = [
        _CREATION_MISSION,
        "",
        *_CREATION_LAYERS,
        "",
        *_conversation_lines(ideas, conversation),
    ]
    if first_turn:
        lines.append("(no agent turn yet)")
    lines.extend(["", *_CREATION_TURN_INSTRUCTIONS])
    return "\n".join(lines)


def _creation_finalize_prompt(
    ideas: str, conversation: Sequence[tuple[str, str]]
) -> str:
    lines = [
        "Produce the FINAL hypothesis agreed in this conversation as a "
        "statistical model that is estimated and falsified on historical data. "
        "Treat the operator's idea and every turn below as the source of truth; "
        "fill any remaining gap with the model you proposed, never with a "
        "placeholder.",
        "",
        *_CREATION_LAYERS,
        "",
        *_conversation_lines(ideas, conversation),
        "",
        *_CREATION_FINALIZE_INSTRUCTIONS,
    ]
    return "\n".join(lines)


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
