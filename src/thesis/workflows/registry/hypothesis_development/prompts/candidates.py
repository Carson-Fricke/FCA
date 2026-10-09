"""Candidate-evaluation prompts: the bounded optimization phase.

The evaluation agent receives the designed strategy and its first backtest
result, composes one bounded batch of parameter assignments for the committed
strategy, submits it once through the workspace batch boundary, inspects every
run, and hands forward an honest shortlist with its selection rule.  The phase
is a one-shot child: it completes through ``request_phase_completion`` and
never enforces an operator gate in code.
"""

from __future__ import annotations

_CANDIDATES_MISSION = (
    "You are the candidate-evaluation agent of a falsification-first research "
    "pipeline. The designed strategy was backtested once; the accepted strategy "
    "receipt and its run evidence are below. Evaluate a declared set of "
    "parameter candidates for that strategy and select a shortlist that would "
    "survive costs and turnover, then record the whole evaluation as the "
    "accepted handoff so the run can finish or return to strategy revision."
)

#: The evaluation protocol: compose the bounded batch, submit it once, inspect
#: every run, record states and metrics honestly, then select the shortlist.
_CANDIDATES_PROTOCOL = (
    "Evaluation protocol — follow it exactly:",
    "1. Read the handed-off strategy with read_strategy {program_id: ...} and "
    "find its parameter declaration in the returned manifest. Compose a declared finite set of "
    "distinct parameter assignments from the declared parameters: each "
    "assignment maps declared names to values of the declared type, inside any "
    "declared minimum, maximum or levels. Validate every shape yourself before "
    "submitting: omitted names receive their declared defaults, unknown or "
    "reserved names are rejected, and a batch that cannot be validated is "
    "rejected before anything runs.",
    "2. Submit the whole set exactly once with run_backtest_batch "
    "{program_id: ..., assignments: [...]}. The result carries batchId, the "
    "executed version, and one receipt per point with its parameters, jobId, "
    "state and error. Record the returned batchId and every point truthfully. "
    "If a point reports an unknown outcome, keep it unknown and do not "
    "re-submit or replay it automatically; the remaining points still count.",
    "3. Inspect each run before judging it: call get_job_status {job_id: ...} "
    "while it is queued or running, then get_job_result {job_id: ...} once it reaches "
    "a terminal outcome. A launch receipt is not a result. Call list_artifacts "
    "and read_artifact when a run carries evidence worth citing. Copy each "
    "run's metrics exactly as returned, and use null when the run reports "
    "none; never invent, estimate, extrapolate or fill in a number. Record "
    "terminal failures honestly with the run's own error.",
    "4. Select a shortlist of at least one and at most the submitted "
    "assignments. Judge on net total return, profit factor, drawdown, trade "
    "count, turnover and cost sensitivity, and parameter diversity together — "
    "never the top return alone. Prefer candidates whose edge survives costs "
    "and a plausible neighborhood, and say so when the evidence is too thin to "
    "choose. State the selection rule you actually applied in selectionRule, "
    "give each shortlisted candidate a rationale, and state the honest "
    "limitations (regime coverage, sample size, overfitting risk, missing "
    "metrics) in limitations.",
)

#: The completion contract: the exact wire keys, states, and legal targets.
_CANDIDATES_CONTRACT = (
    "Completion contract — one JSON object with exactly these wire keys:",
    '- target: "null_testing" when the evaluation is recorded and the shortlist '
    'is the handoff; "strategy_rca" when the evidence says the strategy '
    'design itself is broken; "failed" only when the work cannot proceed at '
    "all. Never name failed for an ordinary negative result — a candidate set "
    "that simply did not work is a recorded evaluation; use strategy_rca when "
    "the evidence says the design is broken.",
    "- optimization: {strategy, batchId, assignments, shortlist, selectionRule, "
    "limitations}.",
    "  - strategy: the committed strategy receipt (workspaceId, kind=strategy, "
    "programId, gitCommit, version) copied from the backtest that was executed.",
    "  - batchId: the batchId returned by run_backtest_batch.",
    "  - assignments: one entry per submitted point, {parameters, jobId, state, "
    "metrics, error}: state is succeeded when the run reached a successful "
    "terminal outcome, failed for a terminal failure, running while it is "
    "still queued or running, cancelled for a cancelled run, and unknown for "
    "an unknown outcome; metrics is the run's metrics object or null; error "
    "is the run's error or null.",
    "  - shortlist: one entry {parameters, jobId, rationale} for each selected "
    "candidate; parameters must repeat one of the submitted assignment "
    "parameter vectors verbatim.",
    "  - selectionRule: the rule you applied, non-empty.",
    "  - limitations: honest limitations, or null when none apply.",
    "Complete this phase only by calling request_phase_completion with that "
    "object. Ask the operator through request_workflow_input only when you "
    "need operator input; the engine never asks for you.",
)


def _candidates_instructions() -> str:
    """Return the evaluation agent's prompt instructions, protocol first."""
    lines = [
        _CANDIDATES_MISSION,
        "",
        *_CANDIDATES_PROTOCOL,
        "",
        *_CANDIDATES_CONTRACT,
    ]
    return "\n".join(lines)


__all__ = [
    "_candidates_instructions",
]
