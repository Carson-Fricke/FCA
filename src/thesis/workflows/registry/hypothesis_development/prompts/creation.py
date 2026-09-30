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
    "pipeline. Turn the operator's intuition into ONE statistical model of the "
    "assets involved and decide, from historical data, whether a regularity "
    "matching the intuition exists. This is research on history: no sizing, "
    "costing, or forward-looking experiments."
)


#: Every operator-facing surface renders KaTeX, so math must be authored as
#: LaTeX rather than plain text or fenced code. The `\(...\)` and `\[...\]`
#: delimiters are used instead of `$` because a bare `$` collides with currency
#: and dollar-denominated numbers.
_LATEX_INSTRUCTION = (
    "Render all math as LaTeX for KaTeX: inline \\(...\\), display \\[...\\] "
    "on its own line; never a bare $, never inside a code fence."
)


#: The completion protocol every creation turn follows. It leads the prompt
#: so the confirmation gate is the first rule the model reads, and it is
#: repeated at the very end so it is also the last. The protocol is a loop:
#: propose and ask in one response, revise and ask again on corrections, and
#: finalize only once the operator confirms.
_CREATION_COMPLETION_PROTOCOL = (
    "Completion protocol — follow it exactly:",
    "1. In ONE response: post the proposal as your reply text AND call "
    'request_workflow_input {"message": "..."} asking the operator to confirm '
    "or revise it. The proposal itself must be the full reply body; the message "
    "is one line asking to confirm it — never a pointer such as 'Proposal "
    "below'. The tool call is what pauses you for the answer; without it the "
    "turn ends and the operator is never asked.",
    "2. While drafting the Rust program you may call validate_study with the "
    'model object itself — validate_study {"model": {"source": "...", '
    '"harnessConfiguration": {"start": "...", "end": "...", "symbols": ["..."]}}} — '
    "to compile-check it; that never replaces the proposal. Do not call run_study, "
    "backtest_config_template, or any backtest tool in this phase: the platform "
    "compiles and runs the study automatically after the operator accepts.",
    "3. On corrections: post the revised proposal AND call "
    "request_workflow_input again in the same response. Repeat until the "
    "operator's latest message confirms.",
    "4. Only on confirmation: call request_phase_completion with the "
    'completion contract below and target "falsification_trials".',
    'Never name target "failed"; if the work cannot proceed, call '
    "request_workflow_input with the reason instead.",
)


#: The final line of the prompt; the last rule the model reads before its
#: first reply.
_CREATION_PROTOCOL_REMINDER = (
    "Reminder: every unconfirmed turn posts the proposal AND calls "
    "request_workflow_input in the same response; call "
    "request_phase_completion only after the operator confirms."
)


#: The two falsification layers the model serves.
_CREATION_LAYERS = (
    "Layer A (now, on history): refute when the model cannot be fit, shows no "
    "regularity beyond a no-structure null, or points the wrong way or too "
    "weak. Markets are noisy: calibrate thresholds with confidence intervals, "
    "bootstrap, or permutation baselines; treat borderline evidence as "
    "inconclusive, never demand strategy-grade performance.",
    "Layer B (later, while a position is open): recompute the same statistics "
    "on a trailing window; leaving their historical range refutes the entry.",
)


_CREATION_TURN_INSTRUCTIONS = (
    "Do this:",
    "1. Restate the intuition statistically; pick the assets and add a "
    "benchmark or control when natural. Infer assets from the idea and propose "
    "a default rather than asking.",
    "2. Choose the estimator, sampling frequency, rolling windows and sample "
    "period with a one-clause reason each. The sample window obeys the "
    "operator: if their message names a time window or horizon (for example "
    "'the last week' or 'since 2020'), harnessConfiguration.start and "
    "harnessConfiguration.end MUST span exactly that window over the most "
    "recent available data — never a longer one. If it is too short to "
    "estimate the model, call request_workflow_input and ask; never expand the "
    "window silently. With no stated window, use the shortest recent window "
    "that fits the model. Declare both bounds as RFC 3339 UTC, end exclusive: "
    "they are exactly what the study fetches. Declare the sampling frequency "
    "as harnessConfiguration.interval, either `1min` or `1d`: studies replay "
    "bars at exactly that interval over the declared window. Never ask the "
    "operator to choose statistics or thresholds.",
    "3. State what the intuition predicts (direction, rough magnitude) and "
    "what a no-structure null would produce instead.",
    "4. Define the Layer A falsifiers — each an observable, a condition (lt, "
    "lte, gt, gte), a threshold and the exact test. Cover fit failure, no "
    "departure from the null, and wrong-direction or too-weak evidence; add at "
    "least one negative control (shuffled returns, unrelated pair, placebo "
    "period) that should show nothing.",
    "5. Say which observables double as Layer B monitors and on what trailing "
    "window. Sizing, costs and execution belong to strategy design.",
    "6. Write the operator-facing proposal as your reply text under '### "
    "Model', '### Data' and '### Falsifiers & Controls'; lead with the model, "
    "not with questions. Model families you may reach for: cross-asset "
    "regressions and cointegration spreads; single-asset autocorrelation, "
    "variance-ratio, Hurst or periodogram tests against a random walk; options "
    "implied-versus-realized volatility, variance risk premia, skew or "
    "term-structure versus realized outcomes.",
    "7. This phase REQUIRES the operator's confirmation before it can "
    "complete: propose and ask in one response, revise and ask again on "
    "corrections, and finalize only after the operator's latest message "
    "confirms. Never fabricate or infer their answer. When finalizing, call "
    "request_phase_completion with the contract at the end of this prompt and "
    "do not restate the model in that reply; the workflow validates and "
    "routes, and you never advance a phase yourself.",
    _LATEX_INSTRUCTION,
)


_CREATION_FINALIZE_INSTRUCTIONS = (
    "The completion contract is one JSON object:",
    "- claim: one sentence stating the regularity the intuition predicts.",
    "- disagreement: one sentence stating what the no-structure null predicts "
    "instead and why the intuition departs from it.",
    "- riskFactor: the main reason the regularity could be spurious or regime-bound.",
    "- horizon: the sampling frequency and estimation window(s); use the "
    "operator's stated window when they name one, otherwise the shortest "
    "recent window that fits the model.",
    "- universe: the ticker symbols, including any benchmark or control.",
    "- falsifiers: each with falsifierId, observable (snake_case), condition "
    "(lt, lte, gt or gte), threshold (number) and test (the exact "
    "computation). Include one that catches an unfit or regularity-free model "
    "(e.g. data_ok < 1.0 or sample_valid < 1.0) and one that catches "
    "wrong-direction or too-weak evidence; note in test which double as "
    "Layer B monitors and on what window.",
    "- body: Markdown with the full specification — variables and "
    "transformations, estimator, expected estimates versus the null, negative "
    "controls, Layer B rule and limitations.",
    "- model (required): a statistical research model, not a strategy, with "
    "modelId, version, description, tests, outputs (the falsifier observable "
    "names as strings, never objects), harnessConfiguration listing every "
    "required symbol plus start and end (RFC 3339 UTC sample bounds, end "
    "exclusive) and interval (`1min` or `1d`; studies replay bars at exactly "
    "this interval over the declared window), and source: a complete Rust "
    "study program against the harness_strategy_sdk crate (Cargo name "
    "harness-strategy-sdk) that compiles and exports the SDK handshake. This "
    "skeleton is the required shape; replace its compile_error! body before "
    "completing, because the unfilled skeleton is rejected by the compile "
    "gate: `use harness_strategy_sdk::{"
    "export_study_program, BarEvent, MetricPublisher, StudyProgram}; use "
    "harness_strategy_sdk::serde_json::Value; struct Model; impl StudyProgram "
    "for Model { fn new(_p: Value) -> Self { Self } fn on_bar(&mut self, "
    "_e: &BarEvent) {} fn on_stop(&mut self, m: &mut MetricPublisher) { "
    'compile_error!("replace this skeleton with the fitted model"); } } '
    "export_study_program!(Model);`. Fold "
    "bars into incremental state, publish every observable by its exact name "
    "with m.publish(name, value) (names must match outputs), and publish at "
    "least one fitted parameter as a fit.<parameter> metric: the trials phase "
    "requires fitted parameters, so a run that publishes none fails the "
    "hypothesis. Also publish failure indicators (e.g. "
    "data_ok=0.0) for missing or inadequate data instead of panicking. No "
    "sizing, no operator-facing prose.",
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
