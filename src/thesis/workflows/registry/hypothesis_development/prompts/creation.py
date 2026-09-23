"""Hypothesis-creation prompts and model-output helpers."""

from __future__ import annotations

from collections.abc import Sequence


def _conversation_lines(conversation: Sequence[tuple[str, str]]) -> list[str]:
    """Render the phase conversation verbatim, starting with its first turn.

    The first non-system entry is the operator's launching message exactly as
    it was written; later entries follow in durable order.
    """
    lines = ["Conversation so far:"]
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


#: The completion protocol every creation turn follows. It leads the prompt
#: so the confirmation gate is the first rule the model reads, and it is
#: repeated at the very end so it is also the last. The protocol is a loop:
#: propose and ask in one response, revise and ask again on corrections, and
#: finalize only once the operator confirms.
_CREATION_COMPLETION_PROTOCOL = (
    "Completion protocol — follow it exactly:",
    "1. In ONE response: write the proposal as your reply text AND call "
    'request_workflow_input {"message": "..."} with a concise request to '
    "confirm or revise it. A response that posts the proposal without that "
    "tool call is incomplete: the turn ends there and the operator is never "
    "asked.",
    "2. Never ask for confirmation in prose only: a prose question is "
    "invisible to the workflow — the operator is asked only when you call "
    "request_workflow_input.",
    "3. If the operator replies with corrections, post the revised proposal "
    "AND call request_workflow_input again, in that same response. Repeat "
    "until the operator's latest message confirms the proposal.",
    "4. Only then finalize: call request_phase_completion with target "
    '"falsification_trials" and the completion contract at the end of this '
    "prompt.",
    'Never name target "failed": that ends the whole workflow. If you believe '
    "the work cannot proceed, say why and call request_workflow_input instead.",
)

#: The final line of the prompt; the last rule the model reads before its
#: first reply.
_CREATION_PROTOCOL_REMINDER = (
    "Reminder: until the operator confirms, every turn posts the (revised) "
    "proposal as reply text AND calls request_workflow_input in the same "
    "response. Only after the operator's latest message accepts it, call "
    'request_phase_completion (target "falsification_trials").'
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
    "one-clause reason for each choice. The sample window derives from the "
    "operator's stated horizon: an operator asking about the last month gets a "
    "model sampled over roughly the last month of available data, so prefer "
    "the most recent available data that satisfies the hypothesis and never "
    "fetch decades of history the hypothesis does not need. Declare that "
    "window in the contract as harnessConfiguration.start and "
    "harnessConfiguration.end (RFC 3339 UTC bounds, end exclusive): those two "
    "values are the exact period the study fetches, so a two-month horizon "
    "declares two months of bounds and never relies on a default period. Do "
    "not ask the "
    "operator which statistics, tests, windows or thresholds to use: that is "
    "your job. Be concrete and creative; if several models fit, name the "
    "primary one and the alternative. Model families you may reach for:",
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
    "6. Write your operator-facing proposal as your reply text, in clear "
    "Markdown under three distinct sections: '### Model' (regularity predicted "
    "vs null, estimator specification, equations), '### Data' (assets/universe, "
    "sampling frequency, sample window, minimum session counts), and "
    "'### Falsifiers & Controls' (Layer A falsifiers with observable/threshold, "
    "negative controls, and Layer B live monitors). Lead with the proposed "
    "model, not with questions. The completion protocol pairs it with the "
    "request_workflow_input call in the same response.",
    "7. This phase REQUIRES the operator's confirmation before it can "
    "complete, exactly as the completion protocol above states: propose and "
    "ask in one response, revise and ask again on corrections, and finalize "
    "only after the operator's latest message confirms. Never fabricate "
    "the operator's answer and never fetch or infer their inputs on their "
    "behalf. When you finalize, call the request_phase_completion tool with "
    "the completion contract listed at the end of this prompt; do not restate "
    "the model in that reply. The workflow validates the contract and routes "
    "the transition, and you never advance a phase yourself.",
    _LATEX_INSTRUCTION,
)


_CREATION_FINALIZE_INSTRUCTIONS = (
    "The hypothesis completion contract is one JSON object with these fields:",
    "- claim: one single-line sentence stating the regularity the intuition "
    "predicts, in model terms.",
    "- disagreement: one single-line sentence stating what the no-structure "
    "null predicts instead and why the intuition departs from it. Without a "
    "departure there is no regularity to trade.",
    "- riskFactor: the main reason the regularity could be spurious or regime-bound.",
    "- horizon: the sampling frequency and the estimation window(s) of the "
    "model. Derive the sample window from the operator's stated horizon: "
    "prefer recent available data covering roughly that horizon, and do not "
    "fetch decades of history the hypothesis does not need.",
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
    "Render its math as LaTeX for KaTeX, exactly as instructed above.",
    "- model (required): a statistical research model, NOT a strategy. Include "
    "modelId, semantic version, description, source defining "
    "fit(context) -> dict[str, number] and observe(fit, context) -> "
    "dict[str, number], fitEntrypoint='fit', observeEntrypoint='observe', "
    "outputs (a JSON array of identifier strings listing every falsifier "
    "observable — the observable names themselves, never objects with a type "
    "field), tests, and harnessConfiguration with "
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


def _creation_turn_prompt(conversation: Sequence[tuple[str, str]]) -> str:
    """Build the phase-1 turn prompt around the operator's launching message.

    The phase instructions frame the system side of the turn; the conversation
    it is handed starts with the operator's message verbatim, which is the turn
    trigger.  No synthesized paraphrase of that message is ever sent.  The
    completion protocol leads and closes the prompt so the confirmation gate
    is both the first and the last rule the model reads.
    """
    lines = [
        _CREATION_MISSION,
        "",
        *_CREATION_COMPLETION_PROTOCOL,
        "",
        *_CREATION_LAYERS,
        "",
        *_conversation_lines(conversation),
        "",
        *_CREATION_TURN_INSTRUCTIONS,
        "",
        "Submit this completion contract when the operator accepts your proposal:",
        *_CREATION_FINALIZE_INSTRUCTIONS,
        "",
        _CREATION_PROTOCOL_REMINDER,
    ]
    return "\n".join(lines)
