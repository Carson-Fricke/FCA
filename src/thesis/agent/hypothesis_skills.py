"""Versioned per-phase skills for the hypothesis-development workflow.

The falsification-first pipeline runs distinct cognitive phases through
distinct Thesis roles.  This module publishes one immutable, versioned
:class:`~thesis.agent.skills.ThesisSkill` per phase and registers them into the
:class:`~thesis.agent.skills.SkillRegistry` alongside ``harness-mcp``:

* ``hypothesis-authoring`` -- form a falsifiable hypothesis with an explicit
  disagreement against market pricing and testable falsifiers.
* ``falsification-planning`` -- design the falsifier trials and the
  negative/placebo controls that separate signal from noise.
* ``falsification-rca`` -- diagnose why a falsification trial failed without
  mutating the premise or the engine.
* ``strategy-authoring`` -- implement a strategy whose exit criteria are
  falsifier re-checks and whose sizing is explicit.

Role policies select the phase skills through
:func:`with_hypothesis_development_skills`: the research role authors and plans,
the critique role diagnoses, and the coding role implements and sizes.
"""

from __future__ import annotations

from dataclasses import replace

from thesis.agent.router import Role, RolePolicy
from thesis.agent.skills import SkillRef, SkillRegistry, ThesisSkill

HYPOTHESIS_AUTHORING_SKILL_ID = "hypothesis-authoring"
FALSIFICATION_PLANNING_SKILL_ID = "falsification-planning"
FALSIFICATION_RCA_SKILL_ID = "falsification-rca"
STRATEGY_AUTHORING_SKILL_ID = "strategy-authoring"
HYPOTHESIS_DEVELOPMENT_SKILL_VERSION = "1.0.0"

_HYPOTHESIS_AUTHORING = (
    "Author a falsifiable market hypothesis. State the claim, the explicit "
    "disagreement with current market pricing that makes the edge tradable, the "
    "risk factor, the horizon, and the universe. Provide at least one "
    "falsifier with an observable metric, a condition, a threshold, and the "
    "test that produces the observation. If the market prices the claim "
    "fairly, there is no trade: the disagreement field must justify why the "
    "price is wrong."
)

_FALSIFICATION_PLANNING = (
    "Plan a real falsification attempt. Bind every falsifier to a concrete "
    "test and add at least one negative or placebo control that should not "
    "show the edge. Prefer the cheapest decisive test first. The plan exists "
    "to destroy the hypothesis, not to confirm it."
)

_FALSIFICATION_RCA = (
    "Diagnose why a falsification trial failed. Separate data, harness, "
    "implementation, strategy, and market-regime causes using the recorded "
    "run artifacts. Cite supporting and disconfirming observations, record "
    "uncertainty, and propose discriminating follow-up tests. Never mutate the "
    "premise or the engine to convert a failure into a pass."
)

_STRATEGY_AUTHORING = (
    "Implement the surviving hypothesis as a strategy whose declared exit "
    "criteria are falsifier re-checks, not profit targets. Make sizing and "
    "risk limits explicit. The strategy is a falsification instrument: an "
    "exit means the trade's micro-hypothesis was falsified, and PnL is only a "
    "guardrail."
)

HYPOTHESIS_DEVELOPMENT_SKILLS: tuple[ThesisSkill, ...] = (
    ThesisSkill(
        skill_id=HYPOTHESIS_AUTHORING_SKILL_ID,
        version=HYPOTHESIS_DEVELOPMENT_SKILL_VERSION,
        instructions=_HYPOTHESIS_AUTHORING,
        allowed_tools=("read_workspace", "run_backtest"),
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
        HYPOTHESIS_AUTHORING_SKILL_ID,
        FALSIFICATION_PLANNING_SKILL_ID,
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
    "FALSIFICATION_PLANNING_SKILL_ID",
    "FALSIFICATION_RCA_SKILL_ID",
    "HYPOTHESIS_AUTHORING_SKILL_ID",
    "HYPOTHESIS_DEVELOPMENT_SKILLS",
    "HYPOTHESIS_DEVELOPMENT_SKILL_VERSION",
    "STRATEGY_AUTHORING_SKILL_ID",
    "hypothesis_development_skill_refs",
    "register_hypothesis_development_skills",
    "with_hypothesis_development_skills",
]
