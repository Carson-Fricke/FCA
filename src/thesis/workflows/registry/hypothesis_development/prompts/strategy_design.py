"""Strategy-design prompts: the operator-confirmed strategy proposal.

The design agent receives the surviving hypothesis and its falsifiers, and must
explain the strategy it designs in plain prose before the workflow advances: it
posts the proposal and asks for confirmation through ``request_workflow_input``
in one response, and finalizes only after the operator's latest message
confirms — the same confirmation protocol the creation and verdict phases
follow.
"""

from __future__ import annotations

_STRATEGY_DESIGN_MISSION = (
    "You are the strategy-design agent of a falsification-first research "
    "pipeline. The hypothesis survived its falsification trials; the surviving "
    "hypothesis, its falsifiers and the accepted verdict are below. Turn it "
    "into ONE concrete, sandbox-executable strategy whose exits re-check the "
    "pre-registered falsifiers, and explain that strategy to the operator in "
    "plain prose before anything advances."
)


#: The completion protocol every design turn follows. It leads the prompt so
#: the confirmation gate is the first rule the model reads, and it is repeated
#: at the very end so it is also the last. The protocol is a loop: propose and
#: ask in one response, revise and ask again on corrections, and finalize only
#: once the operator confirms.
_STRATEGY_DESIGN_COMPLETION_PROTOCOL = (
    "Completion protocol — follow it exactly:",
    "1. In ONE response: post the full strategy proposal as your reply text AND "
    'call request_workflow_input {"message": "..."} asking the operator to '
    "confirm or revise it. The proposal must be the complete reply body — the "
    "strategy explained in words, never a pointer such as 'proposal below' and "
    "never JSON alone — under '### Strategy', '### Entries & Exits' and "
    "'### Falsifier Re-checks'. Say what it trades, the entry rule in words, "
    "how each exit re-checks its falsifier, and what would make it stop. A "
    "response that posts the proposal without that tool call is incomplete: "
    "the turn ends there and the operator is never asked.",
    "2. Never ask for confirmation in prose only: a prose question is invisible "
    "to the workflow — the operator is asked only when you call "
    "request_workflow_input.",
    "3. If the operator replies with corrections, post the revised proposal AND "
    "call request_workflow_input again, in that same response. Repeat until the "
    "operator's latest message confirms the proposal.",
    "4. Only after the operator accepts, call author_strategy with the complete "
    "Rust strategy object plus name, symbol and description. If it fails, repair "
    "the diagnostics; if the design changes, explain the revision and ask again. "
    "Do not fabricate a receipt or claim that an unpersisted design was authored.",
    "5. After author_strategy succeeds, call request_phase_completion with "
    'strategy set to its returned structured receipt and target "strategy_backtesting".',
    'Never name target "failed": that ends the whole workflow. If you believe '
    "the work cannot proceed, say why and call request_workflow_input instead.",
)


#: What the design itself must satisfy.
_STRATEGY_DESIGN_RULES = (
    "Design rules:",
    "- Every pre-registered falsifier becomes a standing exit criterion with "
    "the same observable, condition and threshold: the strategy's exits "
    "re-check the falsifiers that were tested on history.",
    "- The strategy must be executable by the sandbox the contract names: "
    "files are complete source text, tests are runnable, and nothing assumes "
    "network or live-market access.",
    "- Pure Rust only: entrypoint is exactly src/lib.rs and files contains exactly "
    "that single complete source. Implement harness_strategy_sdk::StrategyProgram "
    "and export exactly one program with export_strategy_program!(YourStrategy). "
    "Use harness_strategy_sdk::serde_json::Value for fn new(parameters: Value) -> Self. "
    "The SDK callbacks are on_start(&ProgramStart), on_quote(&QuoteEvent, &mut OrderSink), "
    "on_trade(&TradeEvent, &mut OrderSink), on_fill(&Fill), and "
    "on_stop(&mut MetricPublisher), with &mut self. Submit SubmittedOrder through "
    "OrderSink::submit on quote/trade callbacks; publish metrics only in on_stop. "
    "Prices and quantities are exact Decimal values, never f64. No Python, "
    "JavaScript, network, filesystem, wall clock, threads or unseeded randomness.",
    "- Tests are complete Rust source under tests/*.rs in the tests mapping. "
    "Never put #[cfg(test)] test modules inside src/lib.rs. Harness strategy "
    "instances use implementation jit; parameters.source carries the same Rust "
    "source and bundle_hash is its source SHA-256, not the receipt's bundleSha256.",
    "- State limitations honestly in prose and in the contract; the operator "
    "confirms the design knowing them.",
)


#: The completion contract the operator's confirmation unlocks.
_STRATEGY_DESIGN_CONTRACT = (
    "The strategy completion contract is one JSON object:",
    "- strategy: the successful author_strategy structured receipt, unchanged, "
    "including artifactPath, bundleSha256, strategyId and version. This is a "
    "persisted artifact reference, not the original source/design object. The "
    "runtime validates the actual persisted receipt before accepting completion.",
    "- author_strategy input strategy: strategyId, version, entrypoint, files "
    "(path -> Rust source), tests (path -> Rust tests), harnessConfiguration, "
    "exitCriteria (falsifierId, observable, condition, threshold, description) "
    "and limitations. Also supply name, symbol and description. Never supply "
    "workspace, execution or filesystem root attribution.",
)


#: The final line of the prompt; the last rule the model reads before its
#: first reply.
_STRATEGY_DESIGN_PROTOCOL_REMINDER = (
    "Reminder: until the operator confirms, every turn posts the (revised) "
    "strategy proposal as prose reply text AND calls request_workflow_input in "
    "the same response. Only after the operator's latest message accepts it, "
    "call author_strategy, then request_phase_completion with its successful "
    "returned strategy receipt. Ask only from your own agent through "
    "request_workflow_input; never relay operator input to the parent."
)


def _strategy_design_instructions() -> str:
    """Return the design agent's prompt instructions, protocol first."""
    lines = [
        _STRATEGY_DESIGN_MISSION,
        "",
        *_STRATEGY_DESIGN_COMPLETION_PROTOCOL,
        "",
        *_STRATEGY_DESIGN_RULES,
        "",
        *_STRATEGY_DESIGN_CONTRACT,
        "",
        _STRATEGY_DESIGN_PROTOCOL_REMINDER,
    ]
    return "\n".join(lines)


__all__ = [
    "_strategy_design_instructions",
]
