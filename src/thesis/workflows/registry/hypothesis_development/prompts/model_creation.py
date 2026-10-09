"""Model-creation prompts and model-output helpers.

The model-creation agent receives the discovery phase's selected candidate and
turns it into ONE statistical model with its pre-registered falsifier tests.
Retrieval-first: the model shape comes from the knowledge cards for the
candidate's regularity and the available data intake, not from an inline
family list.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .sdk import SDK_AUTHORING_GUIDANCE


def _conversation_lines(conversation: Sequence[tuple[str, str]]) -> list[str]:
    """Render the phase conversation verbatim, starting with its first turn.

    The first non-system entry is the operator's launching message exactly as
    it was written; later entries follow in durable order.
    """
    lines = ["Conversation so far:"]
    for role, text in conversation:
        lines.append(f"{role}: {text}")
    return lines


#: Shared framing for the model-creation prompts: the agent receives the
#: discovery phase's selected candidate and builds the statistical model that
#: tests it on history.
_MODEL_CREATION_MISSION = (
    "You prepare falsification trials for the accepted strategy sketch. Read its "
    "exact committed revision and identify the assumptions linking mechanism, "
    "representation, conditioning and trading rules. Author one executable study "
    "that can compose the pinned building blocks and publish the observables "
    "needed to challenge those assumptions. The investigation may combine "
    "representations, event/duration analysis, controls and explicit exploratory "
    "models; do not reduce a rich edge to a generic bar-return test. Pre-register "
    "the actual falsifier suite and explain its connection to the sketch. Record "
    "sketchReference unchanged and assumptionTests mapping each investigated "
    "assumptionId to its declared falsifier IDs. Untested assumptions remain "
    "explicit for verdict. This prepares research evidence, not a profitable "
    "strategy verdict. Use the knowledge base for methods, tradeoffs and controls "
    "and exact SDK references for executable capabilities."
)


#: Every operator-facing surface renders KaTeX, so math must be authored as
#: LaTeX rather than plain text or fenced code. The `\(...\)` and `\[...\]`
#: delimiters are used instead of `$` because a bare `$` collides with currency
#: and dollar-denominated numbers.
_LATEX_INSTRUCTION = (
    "Render all math as LaTeX for KaTeX: inline \\(...\\), display \\[...\\] "
    "on its own line; never a bare $, never inside a code fence."
)


#: The completion protocol every model-creation turn follows. It leads the
#: prompt so the confirmation gate is the first rule the model reads, and it is
#: repeated at the very end so it is also the last. The protocol is a loop:
#: propose and ask in one response, revise and ask again on corrections, and
#: finalize only once the operator confirms.
_MODEL_CREATION_COMPLETION_PROTOCOL = (
    "Completion protocol — follow it exactly:",
    "1. In ONE response: post the proposal as your reply text AND call "
    'request_workflow_input {"message": "..."} asking the operator to confirm '
    "or revise it. The proposal itself must be the full reply body; the message "
    "is one line asking to confirm it — never a pointer such as 'Proposal "
    "below'. The tool call is what pauses you for the answer; without it the "
    "turn ends and the operator is never asked.",
    "2. Author or patch the committed study, then call validate_study with "
    "program_id to compile-check its latest content; that never replaces the proposal. Do not call run_study, "
    "backtest_config_template, or any backtest tool in this phase: the platform "
    "runs the accepted committed snapshot automatically after the operator accepts.",
    "3. On corrections: post the revised proposal AND call "
    "request_workflow_input again in the same response. Repeat until the "
    "operator's latest message confirms.",
    "4. Only on confirmation: call request_phase_completion with the "
    'completion contract below and target "falsification_trials".',
    "Submit the full model and falsifier suite the operator approved. If a "
    "tool rejects the argument encoding, repair only the encoding: completion "
    "must be an object, not a JSON string. Never submit a minimal diagnostic "
    "completion, placeholder hypothesis, constant-output stub, or data-only "
    "falsifier suite to test the tool; a valid completion advances the real "
    "workflow. If substantive changes are needed, post the revised proposal "
    "and call request_workflow_input before completing.",
    'Never name target "failed"; if the work cannot proceed, call '
    "request_workflow_input with the reason instead.",
)


#: The final line of the prompt; the last rule the model reads before its
#: first reply.
_MODEL_CREATION_PROTOCOL_REMINDER = (
    "Reminder: every unconfirmed turn posts the proposal AND calls "
    "request_workflow_input in the same response; call "
    "request_phase_completion only after the operator confirms."
)


#: The two falsification layers the model serves.
_MODEL_CREATION_LAYERS = (
    "Layer A (now, on history): refute when the model cannot be fit, shows no "
    "regularity beyond a no-structure null, or points the wrong way or too "
    "weak. Markets are noisy: calibrate thresholds with confidence intervals, "
    "bootstrap, or permutation baselines; treat borderline evidence as "
    "inconclusive, never demand strategy-grade performance.",
    "Layer B (later, while a position is open): recompute the same statistics "
    "on a trailing window; leaving their historical range refutes the entry.",
)


_MODEL_CREATION_TURN_INSTRUCTIONS = (
    "Do this:",
    "1. Translate the sketch assumptions into observable challenges; preserve the representations and conditioning being tested. Pick the assets and add "
    "a benchmark or control when natural. Infer assets from the candidate and "
    "propose a default rather than asking.",
    "2. Choose the estimator, sampling frequency, rolling windows and sample "
    "period with a one-clause reason each. The sample window obeys the "
    "operator: if their message names a time window or horizon (for example "
    "'the last week' or 'since 2020'), committed configuration.start and "
    "configuration.end MUST span exactly that window over the most "
    "recent available data — never a longer one. If it is too short to "
    "estimate the model, call request_workflow_input and ask; never expand the "
    "window silently. With no stated window, use the shortest recent window "
    "that fits the model. Declare both bounds as RFC 3339 UTC, end exclusive: "
    "they are exactly what the study fetches. Declare the sampling frequency "
    "with explicit configuration.subscriptions: each entry has "
    "data_type (bars, quote or trade) and exact symbols; bars alone require "
    "interval (a supported explicit bar cadence, including multiple timeframes when the sketch requires them). No global interval or blanket symbols are "
    "required for tick-only studies. Never ask the "
    "operator to choose statistics or thresholds.",
    "3. State what the candidate predicts (direction, rough magnitude) and "
    "what a no-structure null would produce instead.",
    "4. Define the Layer A falsifiers — each an observable, a condition (lt, "
    "lte, gt, gte), a threshold, the exact test, and an optional role of "
    "claim, assumption or control. Cover all three roles: claim falsifiers "
    "test the candidate's claim; assumption falsifiers check whether the "
    "inference is interpretable (dependence and HAC validity, stationarity, "
    "sample adequacy); control falsifiers are negative controls that should "
    "show nothing. At minimum, cover fit or data failure, departure from the "
    "no-structure null, wrong-direction or too-weak evidence, and one negative "
    "control (shuffled returns, unrelated pair, placebo period) that should "
    "show nothing. Evidence rules: "
    "never use an IID z-statistic on intraday returns — use a HAC estimator or "
    "a bootstrap; when quotes or trades are subscribed, include at least one "
    "economic falsifier (a cost, spread or liquidity condition), not only "
    "statistical ones; and make the pre-registered test account for costs and "
    "timing. Layer A may compute a break-even level from the configured "
    "costs, but never sizing.",
    "5. Say which observables double as Layer B monitors and on what trailing "
    "window. Sizing, costs and execution belong to strategy design.",
    "6. Retrieve the model shape before inventing one: call search_knowledge "
    "for the candidate's regularity shape and the data intake available "
    "(intraday or daily bars, quotes, trades), read_knowledge the returned "
    "cards that matter, and cite their page ids in the proposal body. Assess "
    "relevance: use, adapt, or explicitly reject each retrieved method with a "
    "one-line justification in the proposal. Standard families are allowed as "
    "baselines when they are the right fit for the candidate and data — not "
    "only when search returns nothing; choose the shape that fits the "
    "candidate, not the most familiar one. Standard baselines include: "
    "cross-asset regressions and cointegration spreads; single-asset "
    "autocorrelation, variance-ratio, Hurst or periodogram tests against a "
    "random walk; options implied-versus-realized volatility, variance risk "
    "premia, skew or term-structure versus realized outcomes. Write the "
    "operator-facing proposal as your reply text under '### Model', '### "
    "Data' and '### Falsifiers & Controls'; lead with the model, not with "
    "questions.",
    "7. This phase REQUIRES the operator's confirmation before it can "
    "complete: propose and ask in one response, revise and ask again on "
    "corrections, and finalize only after the operator's latest message "
    "confirms. Never fabricate or infer their answer. When finalizing, call "
    "request_phase_completion with the contract at the end of this prompt and "
    "do not restate the model in that reply; the workflow validates and "
    "routes, and you never advance a phase yourself.",
    _LATEX_INSTRUCTION,
)


_MODEL_CREATION_FINALIZE_INSTRUCTIONS = (
    "The completion contract is one JSON object:",
    "- claim: one sentence stating the regularity the candidate predicts.",
    "- disagreement: one sentence stating what the no-structure null predicts "
    "instead and why the candidate departs from it.",
    "- riskFactor: the main reason the regularity could be spurious or regime-bound.",
    "- horizon: the sampling frequency and estimation window(s); use the "
    "operator's stated window when they name one, otherwise the shortest "
    "recent window that fits the model.",
    "- universe: the ticker symbols, including any benchmark or control.",
    "- falsifiers: each with falsifierId, observable (snake_case), condition "
    "(lt, lte, gt or gte), threshold (number), test (the exact computation), "
    "and an optional role (claim, assumption or control). Cover all three "
    "roles: claim tests the claim, assumption checks the inference is "
    "interpretable (dependence/HAC validity, stationarity, sample adequacy), "
    "and control is a negative control that should show nothing. Include at "
    "least one negative control, one test that catches an unfit or "
    "regularity-free model (e.g. data_ok < 1.0 or sample_valid < 1.0), and one "
    "that catches wrong-direction or too-weak evidence; note in test which "
    "double as Layer B monitors and on what window.",
    "- body: Markdown with the full specification — variables and "
    "transformations, estimator, expected estimates versus the null, negative "
    "controls, Layer B rule and limitations.",
    "- model (required): workspaceId, kind=study, programId, gitCommit and "
    "version (the content hash), plus description, outputs (observable names "
    "as strings) and tests (a non-empty mapping of portable relative file "
    'paths to test text, e.g. {"data_quality.rs": "..."}; a key is a file '
    "path such as data_quality.rs, never a condition or expression like "
    "fit.data_ok < 1.0). Copy the "
    "reference fields from successful validation; do not include source or "
    "harnessConfiguration. Configuration is committed by author_study/update_study "
    "and loaded from that exact gitCommit, never latest after acceptance. "
    "All subscribed symbols must be in universe. Publish every output by its "
    "exact name and at least one fit.<parameter> metric in the authored program. "
    "Publish failure indicators for inadequate data rather than panicking. "
    "No sizing, no operator-facing prose in the program.",
)


def _model_creation_instructions() -> str:
    """Return the model-creation agent's full instructions, protocol first.

    Used when the phase is entered fresh from discovery: the accepted
    discovery handoff is appended to this text by the workflow seed, so the
    instructions themselves carry no conversation.
    """
    lines = [
        _MODEL_CREATION_MISSION,
        "",
        *_MODEL_CREATION_COMPLETION_PROTOCOL,
        SDK_AUTHORING_GUIDANCE,
        "",
        *_MODEL_CREATION_LAYERS,
        "",
        *_MODEL_CREATION_TURN_INSTRUCTIONS,
        "",
        "Submit this completion contract when the operator accepts your proposal:",
        *_MODEL_CREATION_FINALIZE_INSTRUCTIONS,
        "",
        _MODEL_CREATION_PROTOCOL_REMINDER,
    ]
    return "\n".join(lines)


def _model_creation_turn_prompt(conversation: Sequence[tuple[str, str]]) -> str:
    """Build the model-creation turn prompt around a supplied conversation.

    The phase instructions frame the system side of the turn; the conversation
    it is handed starts with the operator's message verbatim, which is the turn
    trigger.  No synthesized paraphrase of that message is ever sent.  The
    completion protocol leads and closes the prompt so the confirmation gate
    is both the first and the last rule the model reads.
    """
    lines = [
        _MODEL_CREATION_MISSION,
        "",
        *_MODEL_CREATION_COMPLETION_PROTOCOL,
        SDK_AUTHORING_GUIDANCE,
        "",
        *_MODEL_CREATION_LAYERS,
        "",
        *_conversation_lines(conversation),
        "",
        *_MODEL_CREATION_TURN_INSTRUCTIONS,
        "",
        "Submit this completion contract when the operator accepts your proposal:",
        *_MODEL_CREATION_FINALIZE_INSTRUCTIONS,
        "",
        _MODEL_CREATION_PROTOCOL_REMINDER,
    ]
    return "\n".join(lines)


def _model_creation_revision_section(
    verdict: Mapping[str, object] | None,
    report: Mapping[str, object] | None,
) -> str:
    """Frame a loop-back re-entry around the recorded trial findings.

    The re-entered phase reuses the same conversation, so the original
    instructions, the previous proposal, and the operator back-and-forth are
    already in its transcript.  The brief therefore carries only what is new:
    the verdict agent's outcome and reason, the recorded falsification-trial
    findings, and the standing obligation to confirm each revision with the
    operator before completing.
    """
    lines = [
        "## Loop-back revision",
        "The operator sent this run back from the verdict: the falsification "
        "trials did not support the previous attempt, and its completion is "
        "superseded. Your original instructions, the previous proposal, and "
        "the operator back-and-forth are earlier in this conversation; do NOT "
        "reply that the phase is complete or that no further action is "
        "required. Revise the model over the still-selected discovery candidate "
        "so the findings below are addressed (or replace it with a "
        "better-supported one over the same operator window). Keep using "
        "request_workflow_input to confirm each revision with the operator and "
        "iterate on the findings until they accept; only then call "
        "request_phase_completion.",
        "For the newly authored model, every revised falsifier observable must "
        "be in the new model.outputs and be published under that exact literal "
        "name. Check the new source and outputs together; the previous model's "
        "outputs do not satisfy this contract.",
        "Post the revised proposal as your reply text AND call "
        "request_workflow_input in the same response; revise and ask again "
        "on operator corrections.",
        "Argument repair must preserve the full revised model and falsifier "
        "suite the operator approved. Never replace them with a minimal "
        "diagnostic object or a data-only suite; that advances a real run.",
    ]
    verdict_payload = _verdict_payload(verdict)
    outcome = verdict_payload.get("outcome")
    reason = verdict_payload.get("reason")
    if isinstance(outcome, str) and outcome:
        verdict_line = f"Verdict: {outcome}"
        if isinstance(reason, str) and reason:
            verdict_line += f" — {reason}"
        lines += ["", verdict_line]
    elif isinstance(reason, str) and reason:
        lines += ["", f"Verdict reason: {reason}"]
    findings = _trial_findings(report)
    if findings:
        lines += ["", "Falsification trials findings:", *findings]
    return "\n".join(lines)


def _verdict_payload(verdict: Mapping[str, object] | None) -> Mapping[str, object]:
    """Return the verdict object from a loop-back completion payload."""
    if not verdict:
        return {}
    nested = verdict.get("verdict")
    return nested if isinstance(nested, Mapping) else verdict


def _trial_findings(report: Mapping[str, object] | None) -> list[str]:
    """Render the recorded trials as compact, one-line findings.

    The trial descriptions are deliberately omitted: the agent authored its
    own falsifiers, so their full definitions are already in its transcript and
    only the recorded outcome is new information for the revision.
    """
    if not report:
        return []
    lines: list[str] = []
    summary = report.get("content")
    if isinstance(summary, str) and summary.strip():
        lines.append(summary.strip())
    trials = report.get("trials")
    if isinstance(trials, list):
        for trial in trials:
            if not isinstance(trial, Mapping):
                continue
            status = "FIRED (refutes)" if trial.get("falsified") else "survived"
            lines.append(
                f"- {trial.get('testId')} {trial.get('falsifierId')}: "
                f"{trial.get('observable')} observed {trial.get('observed')} "
                f"vs {trial.get('condition')} {trial.get('threshold')} "
                f"— {status}"
            )
    metrics = report.get("metrics")
    if isinstance(metrics, Mapping) and metrics:
        published = ", ".join(
            f"{name}={value}" for name, value in sorted(metrics.items())
        )
        lines.append(f"Fitted metrics: {published}")
    return lines


__all__ = [
    "_LATEX_INSTRUCTION",
    "_model_creation_instructions",
    "_model_creation_revision_section",
    "_model_creation_turn_prompt",
]
