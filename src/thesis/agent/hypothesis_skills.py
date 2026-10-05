"""Versioned per-phase skills for the hypothesis-development workflow.

The falsification-first pipeline runs distinct cognitive phases through
distinct Thesis roles.  This module publishes one immutable, versioned
:class:`~thesis.agent.skills.ThesisSkill` per phase and registers them into the
:class:`~thesis.agent.skills.SkillRegistry` alongside ``harness-mcp``:

* ``hypothesis-authoring`` -- RETIRED: persisted references keep resolving, but
  the research role no longer selects it.  The workflow starts in discovery.
* ``discovery`` -- find ONE candidate opportunity with cheap targeted studies,
  logging every attempt and the main competing explanation.
* ``model-creation`` -- turn the selected discovery candidate into one
  statistical model of the assets involved, with Layer A falsifiers that
  double as Layer B monitors.
* ``falsification-planning`` -- design the falsifier trials and the
  negative/placebo controls that separate signal from noise.
* ``falsification-rca`` -- diagnose why a failed harness run (currently the
  strategy backtest) went wrong without mutating the premise or the engine.
  Falsification-trial failures are evaluated agent-first in the trials prose,
  not by this skill.
* ``strategy-authoring`` -- implement a strategy whose exit criteria are
  falsifier re-checks and whose sizing is explicit.

Role policies select the phase skills through
:func:`with_hypothesis_development_skills`: the research role discovers and
models, the critique role diagnoses, and the coding role implements and sizes.
"""

from __future__ import annotations

from dataclasses import replace

from thesis.agent.router import Role, RolePolicy
from thesis.agent.skills import SkillRef, SkillRegistry, ThesisSkill

HYPOTHESIS_AUTHORING_SKILL_ID = "hypothesis-authoring"
DISCOVERY_SKILL_ID = "discovery"
MODEL_CREATION_SKILL_ID = "model-creation"
FALSIFICATION_PLANNING_SKILL_ID = "falsification-planning"
FALSIFICATION_RCA_SKILL_ID = "falsification-rca"
STRATEGY_AUTHORING_SKILL_ID = "strategy-authoring"
HYPOTHESIS_DEVELOPMENT_SKILL_VERSION = "1.4.0"

_DISCOVERY = (
    "Find ONE candidate opportunity worth modeling from the operator's "
    "premise. Run small, cheap, targeted studies against historical data: one "
    "question per study, the smallest informative slice, and a comparable "
    "no-structure baseline. Record every attempt, changed or not, with its "
    "purpose, study id, data types, window and result; a failed or "
    "inconclusive study is a finding, never a hidden one. Propose the "
    "candidate with its signal, information time, direction, holding period, "
    "profit source, measured effect, and the main competing explanation. "
    "Discovery is not validation: never run a backtest, fit the final model, "
    "or pre-register falsifier thresholds here; model creation does that. "
    "Search the knowledge cards for methods and data caveats when useful, and "
    "cite the page ids you use."
)

_MODEL_CREATION = (
    "Turn the discovery phase's selected candidate into ONE statistical model "
    "of the assets involved and pre-register the falsifier tests that would "
    "refute it, estimated on historical data. Retrieve the model shape first: "
    "search the knowledge cards for the candidate's regularity and the "
    "available data intake, read the top card, and cite its page id in the "
    "body. Provide falsifiers with an observable statistic, a condition, a "
    "threshold, and the computation that produces it; never use an IID "
    "z-statistic on intraday returns (use a HAC estimator or a bootstrap), "
    "include at least one economic falsifier when quotes or trades are "
    "subscribed, and account for costs and timing without sizing. The model "
    "source is an authored Rust study program compiled by the harness JIT "
    "that publishes every declared output under its exact literal name and at "
    "least one fit.<parameter> metric because the trials phase requires "
    "fitted parameters. A fired falsifier refutes the claim on history "
    "(Layer A); the same published statistic on a trailing window becomes the "
    "strategy's Layer B monitor. Sizing, costs, and execution belong to "
    "strategy design."
)

_HYPOTHESIS_AUTHORING = (
    "Turn the operator's intuition about a trading opportunity into one "
    "statistical model of the assets involved and decide, from historical "
    "data, whether a regularity matching the intuition exists. Propose the "
    "model yourself (assets, estimator, sampling frequency, windows, sample "
    "period) instead of asking which statistics to use; never propose a "
    "forward-looking experiment. State the claim as the regularity the model "
    "predicts, the disagreement as what a no-structure null predicts instead, "
    "the risk factor, the horizon, and the universe. Provide falsifiers with an "
    "observable statistic, a condition, a threshold, and the computation that "
    "produces it. The model source is an authored Rust study program compiled "
    "by the harness JIT: implement MarketConsumer with type Context = "
    "ObservationContext, subscriptions(&self) -> Vec<Subscription>, and "
    "on_bar(&BarFrame, &mut ObservationContext), on_quote(&QuoteEvent, "
    "&mut ObservationContext), or on_trade(&TradeEvent, &mut ObservationContext). "
    "Separately implement harness_strategy_sdk::StudyProgram with "
    "new(parameters) and on_stop(&mut MetricPublisher), "
    "and export it with export_study_program!. Each atomic BarFrame contains "
    "interval_start_ns, interval_end_ns, available_at_ns, bars_by_symbol and "
    "missing_symbols. Fold same-interval bars from bars_by_symbol, with their "
    "exact open, high, low, close and volume decimals, into incremental "
    "state. Handle missing_symbols explicitly: never align by row index or "
    "silently carry a previous close. Frames become available at interval end "
    "under the historical end-of-interval availability assumption. Keep "
    "publishing every falsifier observable by name in on_stop via "
    "metrics.publish(name, value); the names must match the declared outputs, "
    "and at least one fitted parameter must be published as a fit.<parameter> "
    "metric because the trials phase requires fitted parameters. The study "
    "declares explicit harnessConfiguration.subscriptions with data_type "
    "bars, quote or trade and exact symbols; only bars require interval "
    "(1min or 1d). Tick-only studies need no global interval. "
    "Use one bars interval across all subscriptions and canonical uppercase "
    "market symbols, preserving BTC-USD or BTC/USD as declared. Any legacy "
    "interval must match that bars interval (never for tick-only); any global "
    "symbols must equal the exact subscription union. Source "
    "subscriptions are authoritative; the configuration echo must match their "
    "union including nested owned indicators exposed through indicators(). "
    "Indicators implement MarketConsumer<Context = ObservationContext> and "
    "Indicator::value; the SDK composes and routes dependencies automatically. "
    "A fired falsifier refutes the claim "
    "on history (Layer A); the same published statistic on a trailing window "
    "becomes the strategy's Layer B monitor. Sizing, costs, and execution "
    "belong to strategy design."
)

_FALSIFICATION_PLANNING = (
    "Plan a real falsification attempt. Bind every falsifier to a concrete "
    "test and add at least one negative or placebo control that should not "
    "show the edge. Prefer the cheapest decisive test first. The plan exists "
    "to destroy the hypothesis, not to confirm it."
)

_FALSIFICATION_RCA = (
    "Diagnose why a recorded harness run failed. Separate data, harness, "
    "implementation, strategy, and market-regime causes using the recorded "
    "run artifacts and the exact failure message. Cite supporting and "
    "disconfirming observations, record uncertainty, and propose discriminating "
    "follow-up tests. Never mutate the premise or the engine to convert a "
    "failure into a pass."
)

_STRATEGY_AUTHORING = (
    "Implement the surviving hypothesis as an authored Rust strategy program: "
    "implement MarketConsumer with type Context = OrderSink and explicit "
    "subscriptions; on_quote, on_trade and on_bar take &mut OrderSink. "
    "Implement harness_strategy_sdk::StrategyProgram separately for new, "
    "on_start, on_fill and on_stop, and export it with "
    "export_strategy_program!, submitting orders through the order sink. Make "
    "sizing and risk limits explicit. Any data.subscriptions configuration echo "
    "must match the source union including owned indicators returned by "
    "indicators(); never widen data supplies. Declare exit criteria that are "
    "falsifier re-checks, not profit targets. Reference the surviving workspace "
    "research model and its frozen fit rather than copying the model's "
    "definition into the strategy or re-estimating its parameters. Publish each "
    "Layer B observation under the exact falsifier observable in "
    "on_stop(&mut MetricPublisher) via metrics.publish(name, value). A "
    "falsifier hit must flatten positions and prevent new entries. The strategy "
    "is a falsification instrument: an exit means the trade's micro-hypothesis "
    "was falsified, and PnL is only a guardrail."
)

HYPOTHESIS_DEVELOPMENT_SKILLS: tuple[ThesisSkill, ...] = (
    ThesisSkill(
        skill_id=HYPOTHESIS_AUTHORING_SKILL_ID,
        version=HYPOTHESIS_DEVELOPMENT_SKILL_VERSION,
        instructions=_HYPOTHESIS_AUTHORING,
        allowed_tools=("read_workspace", "run_backtest"),
    ),
    ThesisSkill(
        skill_id=DISCOVERY_SKILL_ID,
        version=HYPOTHESIS_DEVELOPMENT_SKILL_VERSION,
        instructions=_DISCOVERY,
        allowed_tools=("read_workspace", "run_study", "get_job_status", "get_run"),
    ),
    ThesisSkill(
        skill_id=MODEL_CREATION_SKILL_ID,
        version=HYPOTHESIS_DEVELOPMENT_SKILL_VERSION,
        instructions=_MODEL_CREATION,
        allowed_tools=("read_workspace", "validate_study"),
    ),
    ThesisSkill(
        skill_id=FALSIFICATION_PLANNING_SKILL_ID,
        version=HYPOTHESIS_DEVELOPMENT_SKILL_VERSION,
        instructions=_FALSIFICATION_PLANNING,
        allowed_tools=("read_workspace", "run_backtest"),
    ),
    ThesisSkill(
        skill_id=FALSIFICATION_RCA_SKILL_ID,
        version=HYPOTHESIS_DEVELOPMENT_SKILL_VERSION,
        instructions=_FALSIFICATION_RCA,
        allowed_tools=("read_workspace", "get_run", "run_backtest"),
    ),
    ThesisSkill(
        skill_id=STRATEGY_AUTHORING_SKILL_ID,
        version=HYPOTHESIS_DEVELOPMENT_SKILL_VERSION,
        instructions=_STRATEGY_AUTHORING,
        allowed_tools=("read_workspace", "write_patch", "run_backtest"),
    ),
)

_ROLE_SKILL_IDS: dict[Role, tuple[str, ...]] = {
    "research": (
        DISCOVERY_SKILL_ID,
        MODEL_CREATION_SKILL_ID,
    ),
    "critique": (FALSIFICATION_RCA_SKILL_ID,),
    "coding": (STRATEGY_AUTHORING_SKILL_ID,),
}


def hypothesis_development_skill_refs(
    role: Role,
    *,
    version: str = HYPOTHESIS_DEVELOPMENT_SKILL_VERSION,
) -> tuple[SkillRef, ...]:
    """Return the phase skill references the role should select."""
    return tuple(SkillRef(skill_id, version) for skill_id in _ROLE_SKILL_IDS[role])


def register_hypothesis_development_skills(
    registry: SkillRegistry,
    *,
    version: str = HYPOTHESIS_DEVELOPMENT_SKILL_VERSION,
) -> tuple[ThesisSkill, ...]:
    """Register every phase skill version into ``registry`` (idempotent)."""
    registered: list[ThesisSkill] = []
    for skill in HYPOTHESIS_DEVELOPMENT_SKILLS:
        selected = (
            skill
            if version == HYPOTHESIS_DEVELOPMENT_SKILL_VERSION
            else replace(skill, version=version)
        )
        registry.register(selected)
        registered.append(selected)
    return tuple(registered)


def with_hypothesis_development_skills(
    policy: RolePolicy,
    *,
    version: str = HYPOTHESIS_DEVELOPMENT_SKILL_VERSION,
) -> RolePolicy:
    """Return ``policy`` augmented with the phase skills for its role.

    Only roles with declared phase skills are changed; an unknown or primary
    role is returned unchanged.  The harness-mcp skill and its tools are not
    touched, so this composes cleanly with
    :func:`thesis.agent.tool_dispatch.with_harness_mcp`.
    """
    skill_ids = _ROLE_SKILL_IDS.get(policy.role, ())
    if not skill_ids:
        return policy
    existing: dict[str, SkillRef] = {}
    for skill in policy.skills:
        resolved = SkillRef.parse(skill) if isinstance(skill, str) else skill
        existing[resolved.key] = resolved
    references = hypothesis_development_skill_refs(policy.role, version=version)
    for reference in references:
        existing[reference.key] = reference
    skills = tuple(existing[key] for key in sorted(existing))
    allowed = set(policy.allowed_tools)
    for reference in references:
        for skill in HYPOTHESIS_DEVELOPMENT_SKILLS:
            if skill.skill_id == reference.skill_id:
                allowed.update(skill.allowed_tools)
    return replace(policy, skills=skills, allowed_tools=tuple(sorted(allowed)))


__all__ = [
    "DISCOVERY_SKILL_ID",
    "FALSIFICATION_PLANNING_SKILL_ID",
    "FALSIFICATION_RCA_SKILL_ID",
    "HYPOTHESIS_AUTHORING_SKILL_ID",
    "HYPOTHESIS_DEVELOPMENT_SKILLS",
    "HYPOTHESIS_DEVELOPMENT_SKILL_VERSION",
    "MODEL_CREATION_SKILL_ID",
    "STRATEGY_AUTHORING_SKILL_ID",
    "hypothesis_development_skill_refs",
    "register_hypothesis_development_skills",
    "with_hypothesis_development_skills",
]
