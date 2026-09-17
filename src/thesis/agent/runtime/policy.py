"""Spawn authorization and per-role tool policy.

Every sub-agent is created through the ``spawn_sub_agent`` command contract, and
every spawn is authorized against a declarative :class:`SpawnPolicy`.  The policy
is deliberately permissive by default: an undeclared role is allowed, a child
inherits the caller's tools, and root-level spawning is authorized.  The one
property that is always enforced is non-escalation: a child's effective tool set
is the intersection of what it asked for with what its caller may itself call, so
a child can never gain a capability its parent does not hold.  Declared roles may
narrow that further, and a role may declare its own budget ceiling.

Budgets are static ceilings, not consumption ledgers: a child may never be
granted more delegated budget than its caller holds, and a root child is bounded
by the policy's root ceiling.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from thesis.agent.runtime.specs import DEFAULT_MAX_CHILDREN, DEFAULT_MAX_DEPTH
from thesis.chat.contracts import SubAgentRecord

#: The delegated-budget ceiling for a root-level (chat-spawned) child.
DEFAULT_ROOT_BUDGET_CEILING = 1000


class SpawnPolicyError(ValueError):
    """Raised when a spawn policy is malformed."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpawnPolicyError(f"{name} must be non-empty text")
    return value.strip()


@dataclass(frozen=True, slots=True)
class RolePolicySpec:
    """One declared sub-agent role and its optional narrowing.

    ``allowed_tools`` is ``None`` to inherit the caller's effective policy
    unchanged, which is the permissive default.  ``max_delegated_budget`` is
    ``None`` to fall back to the policy-wide ceiling.
    """

    role: str
    allowed_tools: tuple[str, ...] | None = None
    max_delegated_budget: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _text(self.role, "role"))
        if self.allowed_tools is not None:
            tools = tuple(
                _text(tool, "allowed_tools entry") for tool in self.allowed_tools
            )
            object.__setattr__(self, "allowed_tools", tuple(dict.fromkeys(tools)))
        if self.max_delegated_budget is not None and self.max_delegated_budget < 0:
            raise SpawnPolicyError("max_delegated_budget must not be negative")


@dataclass(frozen=True, slots=True)
class SpawnAuthorization:
    """The authorized shape of one spawn command."""

    role: str
    allowed_tools: tuple[str, ...]
    delegated_budget: int
    dropped_tools: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return the canonical authorization record."""
        return {
            "role": self.role,
            "allowedTools": list(self.allowed_tools),
            "delegatedBudget": self.delegated_budget,
            "droppedTools": list(self.dropped_tools),
        }


@dataclass(frozen=True, slots=True)
class SpawnDenial:
    """A refused spawn command with a stable code."""

    code: str
    message: str

    def as_dict(self) -> dict[str, object]:
        """Return the canonical denial record."""
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class SpawnPolicy:
    """Declarative spawn authorization, permissive unless narrowed."""

    roles: tuple[RolePolicySpec, ...] = ()
    allow_unknown_roles: bool = True
    allow_root_spawns: bool = True
    root_budget_ceiling: int = DEFAULT_ROOT_BUDGET_CEILING
    default_max_delegated_budget: int | None = None
    max_depth: int = DEFAULT_MAX_DEPTH
    max_children: int = DEFAULT_MAX_CHILDREN
    extra: Mapping[str, object] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if self.root_budget_ceiling < 0:
            raise SpawnPolicyError("root_budget_ceiling must not be negative")
        if self.default_max_delegated_budget is not None:
            if self.default_max_delegated_budget < 0:
                raise SpawnPolicyError(
                    "default_max_delegated_budget must not be negative"
                )
        if self.max_depth < 1:
            raise SpawnPolicyError("max_depth must be at least one")
        if self.max_children < 1:
            raise SpawnPolicyError("max_children must be at least one")
        seen: set[str] = set()
        for spec in self.roles:
            if spec.role in seen:
                raise SpawnPolicyError(f"duplicate role policy: {spec.role!r}")
            seen.add(spec.role)

    def role_spec(self, role: str) -> RolePolicySpec | None:
        """Return the declared role policy, or ``None`` for an open role."""
        for spec in self.roles:
            if spec.role == role:
                return spec
        return None

    def authorize(
        self,
        *,
        requested_role: str,
        requested_tools: Sequence[str],
        requested_budget: int,
        caller_tools: Sequence[str],
        parent_sub_agent_id: str | None,
    ) -> SpawnAuthorization | SpawnDenial:
        """Authorize one spawn, never widening the caller's capabilities."""
        role = _text(requested_role, "role")
        if parent_sub_agent_id is None and not self.allow_root_spawns:
            return SpawnDenial(
                "root_spawn_denied",
                "root-level sub-agent spawning is not authorized by policy",
            )
        spec = self.role_spec(role)
        if spec is None and not self.allow_unknown_roles:
            return SpawnDenial("unknown_role", f"unknown sub-agent role: {role!r}")
        caller = frozenset(caller_tools)
        requested = frozenset(requested_tools)
        if not requested:
            requested = caller
        allowed = requested if not caller else requested & caller
        if spec is not None and spec.allowed_tools is not None:
            allowed = allowed & frozenset(spec.allowed_tools)
        ceiling = self.default_max_delegated_budget
        if spec is not None and spec.max_delegated_budget is not None:
            ceiling = (
                spec.max_delegated_budget
                if ceiling is None
                else min(ceiling, spec.max_delegated_budget)
            )
        if parent_sub_agent_id is None:
            ceiling = (
                self.root_budget_ceiling
                if ceiling is None
                else min(ceiling, self.root_budget_ceiling)
            )
        budget = requested_budget
        if ceiling is not None:
            budget = min(budget, ceiling)
        return SpawnAuthorization(
            role=role,
            allowed_tools=tuple(sorted(allowed)),
            delegated_budget=budget,
            dropped_tools=tuple(sorted(requested - allowed)),
        )


class OperatorPolicyError(ValueError):
    """Raised when a per-node operator policy is malformed."""


class OperatorActorKind(StrEnum):
    """Who may answer a node's question, as trusted actor kinds."""

    HUMAN_OPERATOR = "human_operator"
    PARENT_AGENT = "parent_agent"
    AGENT = "agent"


#: The default for every node: only the human operator may answer.
HUMAN_ONLY_ACTOR_KINDS: frozenset[OperatorActorKind] = frozenset(
    {OperatorActorKind.HUMAN_OPERATOR}
)


def _actor_kind(value: object) -> OperatorActorKind:
    if isinstance(value, OperatorActorKind):
        return value
    try:
        return OperatorActorKind(value)
    except ValueError as error:
        raise OperatorPolicyError(
            "actor kind must be 'human_operator', 'parent_agent', or 'agent'"
        ) from error


@dataclass(frozen=True, slots=True)
class AnsweringActor:
    """One trusted answerer: an identity plus the kind the runtime derived.

    The identity and kind are built by the trusted caller (the HTTP layer from
    the authenticated session, the runtime from the trusted tool context);
    they are never accepted from model arguments or a request body.
    """

    actor_id: str
    kind: OperatorActorKind = OperatorActorKind.HUMAN_OPERATOR

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_id", _text(self.actor_id, "actor_id"))
        object.__setattr__(self, "kind", _actor_kind(self.kind))

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, JSON-ready answerer record."""
        return {"actorId": self.actor_id, "actorKind": self.kind.value}


@dataclass(frozen=True, slots=True)
class NodeOperatorPolicy:
    """Who may answer one node's question; human-only by default.

    The policy is deliberately not a durable record field: it derives from the
    node's role and is held at the runtime level, so a restart falls back to
    human-only, which is fail-safe.
    """

    allowed_actor_kinds: frozenset[OperatorActorKind] = HUMAN_ONLY_ACTOR_KINDS

    def __post_init__(self) -> None:
        if isinstance(self.allowed_actor_kinds, str):
            raise OperatorPolicyError(
                "allowed_actor_kinds must be a collection of actor kinds"
            )
        object.__setattr__(
            self,
            "allowed_actor_kinds",
            frozenset(_actor_kind(kind) for kind in self.allowed_actor_kinds),
        )

    @property
    def human_only(self) -> bool:
        """Return whether the policy authorizes exactly the human operator."""
        return self.allowed_actor_kinds == HUMAN_ONLY_ACTOR_KINDS

    def permits(self, kind: OperatorActorKind) -> bool:
        """Return whether ``kind`` may answer a node under this policy."""
        return _actor_kind(kind) in self.allowed_actor_kinds

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, JSON-ready policy record."""
        return {
            "allowedActorKinds": sorted(
                kind.value for kind in self.allowed_actor_kinds
            ),
            "humanOnly": self.human_only,
        }


@dataclass(frozen=True, slots=True)
class OperatorPolicyDecision:
    """The adjudicated answer for one node and one answering actor."""

    allowed: bool
    actor: AnsweringActor
    policy: NodeOperatorPolicy
    role: str
    node_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the provenance record persisted with an accepted answer."""
        return {
            "allowed": self.allowed,
            "actorId": self.actor.actor_id,
            "actorKind": self.actor.kind.value,
            "allowedActorKinds": sorted(
                kind.value for kind in self.policy.allowed_actor_kinds
            ),
            "policy": "human_only" if self.policy.human_only else "custom",
            "role": self.role,
            "nodeId": self.node_id,
        }

    def require(self) -> None:
        """Raise :class:`OperatorPolicyDenied` unless the answer is allowed."""
        if not self.allowed:
            raise OperatorPolicyDenied(self)


class OperatorPolicyDenied(PermissionError):
    """Raised when the answering actor is not authorized for the node."""

    code = "operator_policy_denied"

    def __init__(self, decision: OperatorPolicyDecision) -> None:
        self.decision = decision
        super().__init__(
            f"actor {decision.actor.actor_id!r} ({decision.actor.kind.value}) "
            f"may not answer node {decision.node_id or decision.role!r}: "
            f"policy allows {sorted(kind.value for kind in decision.policy.allowed_actor_kinds)}"
        )

    def as_dict(self) -> dict[str, object]:
        """Return the canonical denial, including its provenance."""
        return {"code": self.code, "message": str(self)} | self.decision.as_dict()


@dataclass(frozen=True, slots=True)
class OperatorPolicyRegistry:
    """Resolve a node's operator policy from its role plus runtime overrides.

    Every node defaults to human-only.  A less restrictive policy is a runtime
    configuration change: a role policy or a per-node override.  Overrides are
    process-local by design, so durable records need no new field and a restart
    falls back to the human-only default.
    """

    default: NodeOperatorPolicy = field(default_factory=NodeOperatorPolicy)
    role_policies: Mapping[str, NodeOperatorPolicy] = field(default_factory=dict)
    node_overrides: Mapping[str, NodeOperatorPolicy] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.default, NodeOperatorPolicy):
            raise OperatorPolicyError("default must be a NodeOperatorPolicy")
        for name in ("role_policies", "node_overrides"):
            raw = getattr(self, name)
            if not isinstance(raw, Mapping):
                raise OperatorPolicyError(f"{name} must be a mapping")
            resolved: dict[str, NodeOperatorPolicy] = {}
            for key, policy in raw.items():
                resolved[_text(key, f"{name} key")] = self._policy(policy)
            object.__setattr__(self, name, MappingProxyType(resolved))

    @staticmethod
    def _policy(value: object) -> NodeOperatorPolicy:
        if not isinstance(value, NodeOperatorPolicy):
            raise OperatorPolicyError("policy values must be NodeOperatorPolicy")
        return value

    def for_node(
        self, *, role: str, sub_agent_id: str | None = None
    ) -> NodeOperatorPolicy:
        """Return the node's policy: node override, role policy, or default."""
        name = _text(role, "role")
        if sub_agent_id is not None:
            override = self.node_overrides.get(_text(sub_agent_id, "sub_agent_id"))
            if override is not None:
                return override
        return self.role_policies.get(name, self.default)

    def authorize(
        self,
        actor: AnsweringActor,
        *,
        role: str,
        sub_agent_id: str | None = None,
    ) -> OperatorPolicyDecision:
        """Adjudicate one answerer against the node's policy."""
        policy = self.for_node(role=role, sub_agent_id=sub_agent_id)
        return OperatorPolicyDecision(
            allowed=policy.permits(actor.kind),
            actor=actor,
            policy=policy,
            role=role,
            node_id=sub_agent_id,
        )

    def authorize_node_input(
        self, record: SubAgentRecord, actor: AnsweringActor
    ) -> OperatorPolicyDecision:
        """Adjudicate one answerer against a durable node record."""
        return self.authorize(actor, role=record.role, sub_agent_id=record.sub_agent_id)

    def with_node_policy(
        self, sub_agent_id: str, policy: NodeOperatorPolicy
    ) -> OperatorPolicyRegistry:
        """Return a registry with one node override registered."""
        overrides = dict(self.node_overrides)
        overrides[_text(sub_agent_id, "sub_agent_id")] = self._policy(policy)
        return OperatorPolicyRegistry(
            default=self.default,
            role_policies=self.role_policies,
            node_overrides=overrides,
        )


__all__ = [
    "DEFAULT_ROOT_BUDGET_CEILING",
    "HUMAN_ONLY_ACTOR_KINDS",
    "AnsweringActor",
    "NodeOperatorPolicy",
    "OperatorActorKind",
    "OperatorPolicyDecision",
    "OperatorPolicyDenied",
    "OperatorPolicyError",
    "OperatorPolicyRegistry",
    "RolePolicySpec",
    "SpawnAuthorization",
    "SpawnDenial",
    "SpawnPolicy",
    "SpawnPolicyError",
]
