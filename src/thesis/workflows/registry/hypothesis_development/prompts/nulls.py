"""Null-testing prompts: the block-permutation signal-destruction phase.

The null-testing agent receives the accepted candidate evaluation, picks the
shortlisted candidate's parameters and its observed metrics (running that
candidate once through ``run_backtest`` when the evaluation records no usable
metrics), submits one block-permutation null batch for an explicitly chosen
seed set, inspects every replicate run, and hands forward the observed
statistic, the exceedance count, and the p-value computed honestly from the
returned metrics.  The phase is a one-shot child: it completes through
``request_phase_completion`` and never enforces an operator gate in code.
"""

from __future__ import annotations

_NULLS_MISSION = (
    "You are the null-testing agent of a falsification-first research "
    "pipeline. The accepted candidate evaluation is below: a committed "
    "strategy, the bounded parameter batch, and the selected shortlist. "
    "Destroy the selected signal with a block-permutation null batch — "
    "reorder the return blocks so the serial dependence the candidate relies "
    "on cannot survive — and record the observed statistic, the extremeness "
    "count, and the p-value honestly so the parameter variation can measure "
    "the neighborhood or the run can return to strategy revision."
)

#: The null-testing protocol: pick the candidate, choose the transform
#: explicitly, submit once, inspect every run, and compute the statistics.
_NULLS_PROTOCOL = (
    "Null-testing protocol — follow it exactly:",
    "1. Select the candidate under test from the accepted evaluation: read the "
    "shortlisted entry and reuse its parameters and its jobId. When the "
    "evaluation records usable metrics for that jobId, use those recorded "
    "metrics as the observed metrics — do not rerun the candidate. When the "
    "shortlist or its recorded metrics are missing or unusable, run the "
    "shortlisted candidate exactly once with run_backtest {program_id: ..., "
    "parameters: ...} and use that run's terminal metrics as the observed "
    "metrics. Name one statistic that must appear in the observed metrics and "
    "declare the direction that supports the candidate: upper when a larger "
    "value supports the edge, lower when a smaller value does.",
    "2. Choose the null transform settings explicitly and record them: "
    "block_size (an integer from 1 to 10000; pick a block length that "
    "preserves the returns' intraday or intraweek structure, not a single "
    "bar) and a fixed list of 1 to 16 distinct non-negative integer seeds. "
    "Submit the whole null batch exactly once with run_null_batch "
    "{program_id: ..., parameters: ..., block_size: ..., seeds: [...], label: "
    "...}. The result carries batchId, the executed version, and one point "
    "per seed with its seed, jobId, state and error. If a point reports an "
    "unknown outcome, keep it unknown and never re-submit or replay it "
    "automatically.",
    "3. Inspect every replicate before judging it: call get_job_status "
    "{job_id: ...} while it is queued or running, then get_job_result {job_id: ...} "
    "once it reaches a terminal outcome. A launch receipt is not a result. "
    "Call list_artifacts and read_artifact when a run carries evidence worth "
    "citing. Copy each run's metrics exactly as returned, use null when the "
    "run reports none, and never invent, estimate, extrapolate or fill in a "
    "number. Record terminal failures honestly with the run's own error.",
    "4. Compute the null statistics from the returned metrics alone, over "
    "succeeded replicates only: count one exceedance when an upper-direction "
    "replicate's statistic is greater than or equal to the observed value, or "
    "when a lower-direction replicate's statistic is less than or equal to "
    "it. Set exceedanceCount to that count and pValue to "
    "(1 + exceedanceCount) / (1 + succeededReplicateCount). Require at least "
    "one succeeded replicate; every succeeded replicate's metrics must "
    "contain the statistic. State the honest limitations (few seeds, one "
    "transform, one symbol, one window, untested block lengths) in "
    "limitations.",
)

#: The completion contract: the exact wire keys, cross-checks, and targets.
_NULLS_CONTRACT = (
    "Completion contract — one JSON object with exactly these wire keys:",
    '- target: "variation" when the null test is recorded and the parameter '
    'variation may run; "strategy_rca" when the null evidence says the strategy '
    'design itself is broken; "failed" only when the work cannot proceed at '
    "all. A null test that simply shows the candidate's statistic is "
    "unremarkable is not a design failure and is not a failed phase.",
    "- nullTesting: {strategy, batchId, mode, blockSize, statistic, direction, "
    "observed, replicates, exceedanceCount, pValue, limitations}.",
    "  - strategy: the committed strategy receipt (workspaceId, kind=strategy, "
    "programId, gitCommit, version) from the accepted evaluation.",
    "  - batchId: the batchId returned by run_null_batch.",
    '  - mode: exactly "block_permutation"; blockSize: the integer you submitted.',
    '  - statistic: the metric name under test; direction: "upper" or "lower".',
    "  - observed: {jobId, metrics, value} for the candidate under test, where "
    "value equals metrics[statistic] exactly.",
    "  - replicates: one entry per submitted seed, {seed, jobId, state, "
    "metrics, error}: state is succeeded when the run reached a successful "
    "terminal outcome, failed for a terminal failure, running while it is "
    "still queued or running, cancelled for a cancelled run, and unknown for "
    "an unknown outcome; metrics is "
    "the run's metrics object or null and must contain the statistic when the "
    "replicate succeeded; error is the run's error or null. Seeds are unique.",
    "  - exceedanceCount: the extremeness count over succeeded replicates; "
    "pValue: (1 + exceedanceCount) / (1 + succeededReplicateCount). The "
    "contract is rejected when the count or p-value disagrees with the "
    "returned metrics.",
    "  - limitations: honest limitations, or null when none apply.",
    "Complete this phase only by calling request_phase_completion with that "
    "object. Ask the operator through request_workflow_input only when you "
    "need operator input; the engine never asks for you.",
)


def _nulls_instructions() -> str:
    """Return the null-testing agent's prompt instructions, protocol first."""
    lines = [
        _NULLS_MISSION,
        "",
        *_NULLS_PROTOCOL,
        "",
        *_NULLS_CONTRACT,
    ]
    return "\n".join(lines)


__all__ = [
    "_nulls_instructions",
]
