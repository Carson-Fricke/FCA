"""Strategy-design prompts: the operator-confirmed strategy proposal.

The design agent receives the discovery thesis, the research evidence and the
surviving hypothesis, and must explain the strategy it designs in plain prose
before the workflow advances: it posts the proposal and asks for confirmation
through ``request_workflow_input`` in one response, and finalizes only after
the operator's latest message confirms — the same confirmation protocol the
creation and verdict phases follow.
"""

from __future__ import annotations

from .sdk import SDK_AUTHORING_GUIDANCE

_STRATEGY_DESIGN_MISSION = (
    "You are the strategy-design agent of a falsification-first research "
    "pipeline. The hypothesis survived its falsification trials; the discovery "
    "thesis, the research evidence and the accepted verdict are below. Turn it "
    "into ONE concrete, sandbox-executable strategy with principled trading "
    "exits and explicit assumption monitors drawn from that evidence, and "
    "explain that strategy to the operator in plain prose before anything "
    "advances."
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
    "'### Assumption Monitors'. Say what it trades, the entry rule in words, "
    "which trading exits and assumption monitors it carries (with each "
    "monitor's observable, trailing window, minimum sample size, update "
    "cadence and breach action), and what would make it stop. A response that "
    "posts the proposal without that tool call is incomplete: the turn ends "
    "there and the operator is never asked.",
    "2. Never ask for confirmation in prose only: a prose question is invisible "
    "to the workflow — the operator is asked only when you call "
    "request_workflow_input.",
    "3. If the operator replies with corrections, post the revised proposal AND "
    "call request_workflow_input again, in that same response. Repeat until the "
    "operator's latest message confirms the proposal.",
    "4. Only after the operator accepts, call author_strategy with program_id, "
    "Rust source, configuration and a parameter_declaration for every meaningful "
    "tunable, or update_strategy for an existing ID. Assignments are validated "
    "against the declared parameters. Then "
    "validate_strategy the latest program_id. If it fails, repair "
    "the diagnostics; if the design changes, explain the revision and ask again. "
    "Do not fabricate a receipt or claim that an unpersisted design was authored.",
    "5. After validation succeeds, call request_phase_completion with "
    'strategy set to its reference fields and target "strategy_backtesting".',
    'Never name target "failed": that ends the whole workflow. If you believe '
    "the work cannot proceed, say why and call request_workflow_input instead.",
)


#: What the design itself must satisfy.
_STRATEGY_DESIGN_RULES = (
    "Design rules:",
    "- Separate trading exits from assumption monitors. Trading exits may act "
    "per trade: time stops, profit-taking, inventory or exposure management, "
    "and any falsifier re-check that is causally recomputable while a position "
    "is open. Assumption monitors are standing duties, not per-trade exits: "
    "each names its observable, trailing window, minimum sample size, update "
    "cadence, and breach action (warn, disable entries, de-risk, flatten). "
    "Aggregate or offline statistics — a historical bootstrap p-value, sample "
    "adequacy, a control comparison — are NOT per-trade exits and must be "
    "recorded as monitoring duties explicitly, never silently dropped. Derive "
    "monitors from the handed-off research evidence wherever the observable "
    "permits a causal trailing computation, and reject a monitor whose "
    "observable is not causally recomputable while a position is open.",
    "- The strategy must be executable by the sandbox the contract names: "
    "files are complete source text, tests are runnable, and nothing assumes "
    "network or live-market access.",
    "- Pure Rust only. Use read_sdk_reference for the current strategy template "
    "and exact callback/order signatures. Prices and quantities are exact Decimal "
    "values, never f64. No network, filesystem, wall clock, threads or unseeded randomness.",
    "- Declare exact source subscriptions, e.g. vec![Subscription::Bars { "
    'symbols: vec!["SPY".into()], interval: "1min".into() }, '
    'Subscription::Quote { symbols: vec!["SPY".into()] }]. '
    "The committed configuration.subscriptions echo uses strict "
    '{"data_type":"bars","symbols":["SPY"],"interval":"1min"} and '
    '{"data_type":"quote","symbols":["SPY"]} entries and must canonically '
    "match the source union, including indicator dependencies. Only bars "
    "require an interval; tick-only inputs need no interval. Never "
    "use multiple distinct bars intervals or noncanonical/lowercase symbols. "
    "Preserve BTC-USD or BTC/USD exactly. Interval-only configuration is "
    "rejected; if global symbols are supplied, "
    "they must equal the subscription union. Never "
    "supply undeclared feeds or widen the operator window. Own indicators in "
    "the program and expose them through indicators(&mut self) -> "
    "Vec<&mut dyn Indicator>; indicators implement MarketConsumer with "
    "ObservationContext and Indicator::value, and nested dependencies are "
    "automatically composed and routed before the parent callback.",
    "- Keep Rust tests under tests/*.rs, never #[cfg(test)] modules in program.rs. "
    "Execution configuration and dependency assembly belong to WorkspacePrograms, "
    "not caller-supplied source overrides.",
    "- Declare every meaningful tunable in parameter_declaration: each entry "
    "carries a type (integer, number, boolean or string), a default, and "
    "optional bounds or levels; never use reserved names (source, source_path, "
    "symbols, or *_path/Path). Backtest parameter assignments are validated "
    "against that declaration before submission, and omitted names receive "
    "their declared defaults.",
    "- State limitations honestly in prose and in the contract; the operator "
    "confirms the design knowing them.",
)


#: The completion contract the operator's confirmation unlocks.
_STRATEGY_DESIGN_CONTRACT = (
    "The strategy completion contract is one JSON object:",
    "- strategy: workspaceId, kind=strategy, programId, gitCommit and version "
    "(content hash), copied from successful validate_strategy. No inline source, "
    "paths or legacy strategyId/semantic-version receipts. The runtime verifies "
    "the committed content and its latest stored validation before acceptance.",
)


#: The final line of the prompt; the last rule the model reads before its
#: first reply.
_STRATEGY_DESIGN_PROTOCOL_REMINDER = (
    "Reminder: until the operator confirms, every turn posts the (revised) "
    "strategy proposal as prose reply text AND calls request_workflow_input in "
    "the same response. Only after the operator's latest message accepts it, "
    "author or patch, validate_strategy, then request_phase_completion with its "
    "successful committed reference. Ask only from your own agent through "
    "request_workflow_input; never relay operator input to the parent."
)


def _strategy_design_instructions() -> str:
    """Return the design agent's prompt instructions, protocol first."""
    lines = [
        _STRATEGY_DESIGN_MISSION,
        "",
        *_STRATEGY_DESIGN_COMPLETION_PROTOCOL,
        SDK_AUTHORING_GUIDANCE,
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
