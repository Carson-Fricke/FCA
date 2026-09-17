"""Role-policy helpers for the quant-harness MCP tool surface.

The model router resolves a role's policy into an effective
:class:`~thesis.agent.router.RouteDecision`, including the tools the role is
allowed to call.  These helpers keep the harness MCP skill and its tool surface
wired into role policies without hand-maintaining the tool list:
:func:`harness_mcp_role_policy` builds a policy that selects ``harness-mcp``
and permits :data:`~thesis.agent.harness_tools.HARNESS_TOOL_NAMES`, while
:func:`with_harness_mcp` augments an existing policy.  Tool calls themselves are
dispatched through the unified session registry, not through a second
policy-enforcing dispatcher.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import cast

from thesis.agent.harness_tools import (
    HARNESS_MCP_SKILL_ID,
    HARNESS_MCP_SKILL_VERSION,
    HARNESS_TOOL_NAMES,
)
from thesis.agent.router import Role, RolePolicy
from thesis.agent.skills import SkillRef


def with_harness_mcp(
    policy: RolePolicy,
    *,
    version: str = HARNESS_MCP_SKILL_VERSION,
) -> RolePolicy:
    """Return ``policy`` augmented with the harness MCP skill and its tools."""
    reference = SkillRef(HARNESS_MCP_SKILL_ID, version)
    existing: dict[str, SkillRef] = {}
    for skill in policy.skills:
        resolved = SkillRef.parse(skill) if isinstance(skill, str) else skill
        existing[resolved.key] = resolved
    existing[reference.key] = reference
    skills = tuple(existing[key] for key in sorted(existing))
    tools = tuple(sorted(set(policy.allowed_tools) | set(HARNESS_TOOL_NAMES)))
    return replace(policy, skills=skills, allowed_tools=tools)


def harness_mcp_role_policy(
    role: Role | str,
    *,
    model_profile: str,
    version: str = "1.0.0",
    skills: Iterable[SkillRef | str] = (),
    allowed_tools: Iterable[str] = (),
    skill_version: str = HARNESS_MCP_SKILL_VERSION,
) -> RolePolicy:
    """Build a role policy selecting ``harness-mcp`` and permitting its tools.

    The returned policy merges the caller's skills and tools with the harness
    MCP skill and :data:`HARNESS_TOOL_NAMES`, so the router resolves the
    ``harness-mcp`` bundle and the session registry permits every harness tool.
    """
    base = RolePolicy(
        role=cast(Role, role),
        version=version,
        model_profile=model_profile,
        skills=tuple(skills),
        allowed_tools=tuple(allowed_tools),
    )
    return with_harness_mcp(base, version=skill_version)


__all__ = [
    "harness_mcp_role_policy",
    "with_harness_mcp",
]
