"""Bounded candidate evaluation using terminal batch evidence."""


def _candidates_instructions() -> str:
    """Evaluate the committed strategy and retain the full selection account."""
    return """You own candidate evaluation. Read the accepted strategy, baseline evidence
and parameter declaration. Declare a finite set of distinct, valid parameter
assignments, then submit it once with run_backtest_batch. All points are validated
before submission; omitted parameters use declared defaults.

Completions arrive asynchronously for each retained jobId. Continue useful work or
end your response; do not poll or resubmit. Inspect delivered terminal results and
relevant artifacts. Preserve every assignment, jobId, state, metric and error;
use null for unavailable metrics. Unknown outcomes remain unresolved.

Select a supported shortlist using net returns, drawdown, turnover, costs, support
and parameter diversity. Explain the actual selection rule and uncertainty.
A weak or broken design can return to strategy_rca; a negative result is valid
research evidence.

Complete with target null_testing or strategy_rca and optimization containing:
strategy (the exact committed reference), batchId, assignments (parameters, jobId,
state, metrics, error), shortlist (parameters, jobId, rationale), selectionRule
and limitations. Every shortlisted vector must match a submitted assignment.
"""
