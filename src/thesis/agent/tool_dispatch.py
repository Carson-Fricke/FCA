"""Policy-scoped dispatch of model tool requests to the harness MCP provider.

The model router resolves a role's policy into an effective
:class:`~thesis.agent.router.RouteDecision`, including the tools the role is
allowed to call.  This module is the last mile between that decision and the
:class:`~thesis.agent.harness_tools.HarnessToolProvider`: when the agent or
model requests a tool, :class:`HarnessToolDispatcher` resolves the role,
enforces the resolved ``allowed_tools``, and routes permitted harness tools to
:meth:`HarnessToolProvider.call_tool`.

The policy helpers keep the harness MCP skill and its tool surface wired into
role policies without hand-maintaining the tool list:
:func:`harness_mcp_role_policy` builds a policy that selects ``harness-mcp``
and permits :data:`~thesis.agent.harness_tools.HARNESS_TOOL_NAMES`, while
:func:`with_harness_mcp` augments an existing policy.  Denied, unknown, and
tool-level failures surface as typed
:class:`~thesis.agent.harness_tools.HarnessToolResult` values instead of
unhandled exceptions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import cast

from thesis.agent.harness_tools import (
    HARNESS_MCP_SKILL_ID,
    HARNESS_MCP_SKILL_VERSION,
    HARNESS_TOOL_NAMES,
    HarnessToolFailure,
    HarnessToolProvider,
    HarnessToolResult,
)
from thesis.agent.router import (
    ModelRouter,
    Role,
    RolePolicy,
    RouteDecision,
)
from thesis.agent.skills import SkillRef


@dataclass(frozen=True, slots=True)
class HarnessToolCall:
    """One model-requested harness tool invocation.

    ``arguments`` are the MCP ``tools/call`` arguments; they are copied into an
    immutable mapping so a caller cannot mutate an in-flight request.
    """

    name: str
    arguments: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool name must be non-empty text")
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> HarnessToolCall:
        """Build a call from a decoded model tool request document."""
        name = payload.get("name")
        arguments = payload.get("arguments", {})
        if not isinstance(name, str):
            raise ValueError("tool request requires a string 'name'")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise ValueError("tool request 'arguments' must be an object")
        return cls(name=name, arguments=arguments)

    def as_dict(self) -> dict[str, object]:
        """Return the canonical tool request representation."""
        return {"arguments": dict(self.arguments), "name": self.name}


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
    ``harness-mcp`` bundle and the dispatcher permits every harness tool.
    """
    base = RolePolicy(
        role=cast(Role, role),
        version=version,
        model_profile=model_profile,
        skills=tuple(skills),
        allowed_tools=tuple(allowed_tools),
    )
    return with_harness_mcp(base, version=skill_version)


class HarnessToolDispatcher:
    """Route permitted tool requests to the harness MCP provider.

    The dispatcher is the agent-side execution seam: it resolves the role
    through the same :class:`ModelRouter` that drove the model invocation,
    enforces the resolved ``allowed_tools``, and calls the injected
    :class:`HarnessToolProvider`.  A tool that the role policy does not permit
    is rejected before any transport exchange.
    """

    def __init__(
        self,
        router: ModelRouter,
        provider: HarnessToolProvider,
    ) -> None:
        if not isinstance(router, ModelRouter):
            raise TypeError("router must be a ModelRouter")
        if not isinstance(provider, HarnessToolProvider):
            raise TypeError("provider must be a HarnessToolProvider")
        self._router = router
        self._provider = provider

    @property
    def router(self) -> ModelRouter:
        """Return the policy router used to resolve role permissions."""
        return self._router

    @property
    def provider(self) -> HarnessToolProvider:
        """Return the harness MCP tool provider that executes permitted calls."""
        return self._provider

    def resolve(self, role: Role | str) -> RouteDecision:
        """Resolve a role to its effective policy decision."""
        return self._router.resolve(role)

    def allowed_tools(self, role: Role | str) -> tuple[str, ...]:
        """Return the effective tools permitted for ``role``."""
        return self.resolve(role).allowed_tools

    def dispatch(
        self,
        role: Role | str,
        call: HarnessToolCall | str,
        arguments: Mapping[str, object] | None = None,
    ) -> HarnessToolResult:
        """Execute one permitted harness tool call for ``role``.

        A name not in the role's resolved ``allowed_tools`` is rejected as a
        ``denied`` failure for a published harness tool and an ``unknown_tool``
        failure otherwise.  Permitted calls are delegated to
        :meth:`HarnessToolProvider.call_tool`, whose structured result and typed
        failures pass through unchanged.
        """
        request = (
            HarnessToolCall(call, arguments or {}) if isinstance(call, str) else call
        )
        route = self.resolve(role)
        if request.name not in route.allowed_tools:
            kind = "denied" if request.name in HARNESS_TOOL_NAMES else "unknown_tool"
            message = (
                f"role {role!r} is not permitted to call harness tool {request.name!r}"
                if kind == "denied"
                else f"unknown harness MCP tool: {request.name!r}"
            )
            return HarnessToolResult(
                tool=request.name,
                ok=False,
                failure=HarnessToolFailure(
                    kind=kind,
                    message=message,
                    tool=request.name,
                    payload={"allowedTools": list(route.allowed_tools)},
                ),
            )
        return self._provider.call_tool(request.name, request.arguments)

    def dispatch_many(
        self,
        role: Role | str,
        calls: Iterable[HarnessToolCall],
    ) -> tuple[HarnessToolResult, ...]:
        """Dispatch a model turn's tool requests in order."""
        return tuple(self.dispatch(role, call) for call in calls)

    def close(self) -> None:
        """Close the underlying harness MCP transport."""
        self._provider.close()


__all__ = [
    "HarnessToolCall",
    "HarnessToolDispatcher",
    "harness_mcp_role_policy",
    "with_harness_mcp",
]
