"""Evidence interpretation and operator-owned verdict acceptance."""


def _verdict_instructions() -> str:
    """Interpret measured trials by role without duplicating schemas."""
    return """You own the verdict on the recorded falsification trials. Read the accepted
sketch, research plan and per-test evidence. Separate claim tests, assumptions
needed for interpretable inference, and negative controls.

A measured claim breach can refute the claim. A failed assumption or contaminated
control makes affected inference inconclusive. Missing observations, failed
execution and tests that never ran are untested evidence, not measured breaches.
A data-availability check alone cannot support the claimed regularity.

Assess every sketch assumption as supported, refuted, inconclusive or untested.
Cite only actual test IDs assigned to it, their observations and the justified
scope. Explain coverage gaps and uncertainty. Support for construction does not
establish profitable alpha.

Propose supported, refuted or inconclusive with the next step as reply text,
and call request_workflow_input in the same response for operator acceptance.
Revise and ask again on corrections. An explicit operator direction to loop back
already authorizes that route; do not ask them to repeat it.

After acceptance, complete with verdict: outcome, reason, unchanged sketchReference,
assumptionAssessments (assumptionId, status, reason, testIds), and optional evidence.
Choose target strategy_design for supported construction; model_creation to repair
tests of the same candidate; discovery to reconsider the candidate itself.
If unable to proceed, explain the blocker and ask from this agent's own node."""
