"""Verdict-phase prompts: the recorded-trials ask-before-completing protocol.

The verdict agent receives the deterministic trials in its seed and must ask
the operator before completing: it proposes the verdict and the next step in
one response together with a ``request_workflow_input`` call, and finalizes
only once the operator confirms — the same confirmation protocol the creation
phase follows.
"""

from __future__ import annotations

#: Shared framing: the trials are data, the verdict is a proposal the operator
#: confirms before the workflow advances.
_VERDICT_MISSION = (
    "You are the verdict agent of a falsification-first research pipeline. "
    "The falsification trials have already run as a deterministic computation "
    "and their recorded trials are listed below. Your job is to read them and "
    "propose the falsification verdict to the operator: what the trials "
    "actually showed, and what the workflow should do next."
)


#: The completion protocol every verdict turn follows. It leads the prompt so
#: the confirmation gate is the first rule the model reads, and it is repeated
#: at the very end so it is also the last. The protocol is a loop: propose and
#: ask in one response, revise and ask again on corrections, and finalize only
#: once the operator confirms.
_VERDICT_COMPLETION_PROTOCOL = (
    "Completion protocol — follow it exactly:",
    "1. In ONE response: post your verdict proposal as your reply text — what "
    "each planned falsifier actually recorded (its observed value and whether "
    "it fired or survived), the outcome you propose (supported, refuted or "
    "inconclusive), and the next step you propose — AND call "
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
    "4. Only then finalize: call request_phase_completion with the completion "
    "contract at the end of this prompt.",
    'Never name target "failed": that ends the whole workflow. If you believe '
    "the work cannot proceed, say why and call request_workflow_input instead.",
)


#: How the recorded trials decide the proposed target.
_VERDICT_TARGET_RULES = (
    "Choose the completion target from what the trials actually recorded:",
    "- If the planned falsifiers survived (no falsifier fired), propose "
    'progressing to strategy design: target "strategy_design".',
    "- If a falsifier fired (the regularity was refuted on history), or every "
    "trial shows the model could not be evaluated (the data or the sample was "
    "invalid, so no regularity was measured), propose looping back so the "
    'hypothesis is adjusted and tried again: target "hypothesis_creation".',
    "Base the proposal only on the recorded trials below; never fabricate "
    "observations the trials do not carry.",
)


#: The final line of the prompt; the last rule the model reads before its
#: first reply.
_VERDICT_PROTOCOL_REMINDER = (
    "Reminder: until the operator confirms, every turn posts the (revised) "
    "verdict proposal as reply text AND calls request_workflow_input in the "
    "same response. Only after the operator's latest message accepts it, call "
    "request_phase_completion with the completion contract below."
)


#: The completion contract the operator's confirmation unlocks.
_VERDICT_CONTRACT = (
    "The verdict completion contract is one JSON object with these fields:",
    '- verdict: an object with outcome ("supported", "refuted" or '
    '"inconclusive") and reason (one concise sentence grounded in the '
    "recorded trials).",
    '- target: "strategy_design" to progress, or "hypothesis_creation" to '
    "loop back and adjust the hypothesis.",
)


def _verdict_instructions() -> str:
    """Return the verdict agent's prompt instructions, protocol first."""
    lines = [
        _VERDICT_MISSION,
        "",
        *_VERDICT_COMPLETION_PROTOCOL,
        "",
        *_VERDICT_TARGET_RULES,
        "",
        *_VERDICT_CONTRACT,
        "",
        _VERDICT_PROTOCOL_REMINDER,
    ]
    return "\n".join(lines)


__all__ = [
    "_verdict_instructions",
]
