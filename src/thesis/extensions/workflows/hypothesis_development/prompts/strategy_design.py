"""Strategy construction from accepted evidence, with conversational review."""

from .sdk import SDK_AUTHORING_GUIDANCE


def _strategy_design_instructions() -> str:
    """Describe construction and one operator-acceptance protocol."""
    return (
        """You own strategy design. Read the exact accepted sketch and evidence. Reuse
sketch.prototype's committed strategy and selected parameters when present; inspect
its executed source and search account. Preserve its mechanism, conditioning and
meaningful feature roles. Explain material departures and their evidence.

Produce one executable strategy with explicit entries, exits, risk, abstention
and assumption monitors. Make tunable parameters match the accepted policy.
Set selected prototype parameters as declaration defaults so the baseline evaluates
that policy. Keep development/forward bounds and information timing unchanged.

Present the complete proposal as ordinary reply text and, in the same response,
call request_workflow_input to request acceptance. Incorporate corrections and
ask again. Author/update and validate the accepted implementation after the operator
accepts; reusing source still requires current validation. Ask from this agent's
own node. Routine implementation choices within the accepted proposal need no
additional confirmation.

Use the engine's execution state for positions, orders, fills, costs and accounting.
Match supported SDK semantics and realistic configuration to the proposed strategy.
Report a technical blocker explicitly rather than treating it as an adverse result.

Complete with strategy containing workspaceId, kind=strategy, programId, gitCommit
and version copied from successful validate_strategy. The accepted tool reference
identifies the implementation; prose alone cannot complete the phase.
\n"""
        + SDK_AUTHORING_GUIDANCE
    )


__all__ = ["_strategy_design_instructions"]
