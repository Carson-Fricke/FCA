"""Versioned per-phase skills for the hypothesis-development workflow.

The falsification-first pipeline runs distinct cognitive phases through
distinct Thesis roles.  This module publishes one immutable, versioned
:class:`~thesis.agent.skills.ThesisSkill` per phase and registers them into the
:class:`~thesis.agent.skills.SkillRegistry` alongside ``harness-mcp``:

* ``discovery`` -- develop a committed strategy sketch using the executable
  knowledge-base tool bag, inspect rich outputs, and preserve negative findings.
* ``model-creation`` -- turn the selected discovery candidate into one
  statistical model of the assets involved, with Layer A falsifiers that
  double as Layer B monitors.
* ``falsification-planning`` -- design the falsifier trials and the
  negative/placebo controls that separate signal from noise.
* ``falsification-rca`` -- diagnose why a failed harness run (currently the
  strategy backtest) went wrong without mutating the premise or the engine.
  Falsification-trial failures are evaluated agent-first in the trials prose,
  not by this skill.
* ``strategy-authoring`` -- implement a strategy with trading exits and
  explicit assumption monitors derived from the research evidence, and
  explicit sizing.

Role policies select the phase skills through
:func:`with_hypothesis_development_skills`: the research role discovers and
models, the critique role diagnoses, and the coding role implements and sizes.
"""

from __future__ import annotations

from dataclasses import replace

from thesis.agent.router import Role, RolePolicy
from thesis.agent.skills import SkillRef, SkillRegistry, ThesisSkill

DISCOVERY_SKILL_ID = "discovery"
MODEL_CREATION_SKILL_ID = "model-creation"
FALSIFICATION_PLANNING_SKILL_ID = "falsification-planning"
FALSIFICATION_RCA_SKILL_ID = "falsification-rca"
STRATEGY_AUTHORING_SKILL_ID = "strategy-authoring"
HYPOTHESIS_DEVELOPMENT_SKILL_VERSION = "1.10.0"

_DISCOVERY = (
    "Develop executable training candidates using the operator's suggested features "
    "in meaningful interacting roles. Keep an evolving committed strategy sketch. "
    "Build/refine, explore parameters and policy variants, then assess after-cost "
    "robustness and appropriate null evidence. Use strategy backtests/batches for "
    "policies and studies for targeted diagnostics; forward data stays sealed. "
    "A fixed searched winner's null is conditional, not search-adjusted evidence. "
    "Retain all attempts and preserve selected prototype references/parameters. "
    "Review intent -> actual test -> justified decision; non-significance or an "
    "omitted interaction cannot refute a feature family. No forced positive finding. "
    "Delegate consequential directions through research-investigation, launch "
    "complementary branches concurrently and triage returns asynchronously. Only "
    "discovery edits the shared sketch. Incorporate operator steering without "
    "discarding relevant work; pending jobs retain their original questions/IDs. "
    "Ask acceptance of the current integrated revision through request_workflow_input "
    "before completing. Read detailed workflow instructions and knowledge by need."
)

_MODEL_CREATION = (
    "During model creation (not discovery), resolve the exact accepted "
    "strategy sketch and design targeted assumption "
    "investigations. Map assumptions to pre-registered falsifier tests that would "
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
    "indicators(); never widen data supplies. Consume the handed-off discovery "
    "thesis and research evidence: separate trading exits (time stops, "
    "profit-taking, inventory or exposure management, and causally "
    "recomputable falsifier re-checks) from assumption monitors (each naming "
    "its observable, trailing window, minimum sample size, update cadence and "
    "breach action: warn, disable entries, de-risk or flatten). Aggregate or "
    "offline statistics are monitoring duties, not per-trade exits; never "
    "silently drop them. Reference the surviving workspace research model and "
    "its frozen fit rather than copying the model's definition into the "
    "strategy or re-estimating its parameters. Publish each Layer B "
    "observation under the exact falsifier observable in "
    "on_stop(&mut MetricPublisher) via metrics.publish(name, value). A "
    "monitor breach must disable new entries and flatten or de-risk as its "
    "declared breach action says. The strategy is a falsification instrument: "
    "exits and monitors serve the hypothesis, and PnL is only a guardrail."
)

HYPOTHESIS_DEVELOPMENT_SKILLS: tuple[ThesisSkill, ...] = (
    ThesisSkill(
        skill_id=DISCOVERY_SKILL_ID,
        version=HYPOTHESIS_DEVELOPMENT_SKILL_VERSION,
        instructions=_DISCOVERY,
        allowed_tools=(
            "read_workspace",
            "run_study",
            "get_job_status",
            "get_job_result",
        ),
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
        allowed_tools=("read_workspace", "get_job_result", "run_backtest"),
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
    "HYPOTHESIS_DEVELOPMENT_SKILLS",
    "HYPOTHESIS_DEVELOPMENT_SKILL_VERSION",
    "MODEL_CREATION_SKILL_ID",
    "STRATEGY_AUTHORING_SKILL_ID",
    "hypothesis_development_skill_refs",
    "register_hypothesis_development_skills",
    "with_hypothesis_development_skills",
]
