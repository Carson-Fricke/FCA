"""Declarative agent specifications and budgets for durable sub-agents.

A sub-agent is created from an immutable :class:`SubAgentSpec`.  The spec is the
durable contract a caller persists alongside the child record: it fixes the
phase kind, the role, the default-deny tool surface, the completion contract,
and the delegated budget.  Deterministic work is never expressed here; a
deterministic phase runs as code and owns no sub-agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from thesis.chat.contracts import ChatContractError, SubAgentKind

#: Default tree guards, enforced deterministically by the spawn command.
DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_CHILDREN = 8

#: Construction repair budget: three attempts per agent turn, two repair turns.
DEFAULT_CONSTRUCTION_ATTEMPTS = 3
DEFAULT_REPAIR_TURNS = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class SubAgentBudget:
    """Budgets inherited from the parent and enforced by the agent session.

    ``construction_attempts`` bounds failed completion-contract constructions in
    one agent turn; ``repair_turns`` bounds the durable repair turns that follow
    an agent turn which ended without an accepted completion.
    """

    token_budget: int | None = None
    tool_iterations: int | None = None
    wallclock_seconds: int | None = None
    construction_attempts: int = DEFAULT_CONSTRUCTION_ATTEMPTS
    repair_turns: int = DEFAULT_REPAIR_TURNS

    def __post_init__(self) -> None:
        for name in ("token_budget", "tool_iterations", "wallclock_seconds"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ChatContractError(f"{name} must not be negative")
        if self.construction_attempts < 1:
            raise ChatContractError("construction_attempts must be at least one")
        if self.repair_turns < 0:
            raise ChatContractError("repair_turns must not be negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class SubAgentSpec:
    """The immutable specification a sub-agent is spawned from."""

    role: str
    kind: SubAgentKind = SubAgentKind.CONVERSATION
    spec_version: str = "1.0.0"
    allowed_tools: tuple[str, ...] = ()
    input_contract_ref: str | None = None
    completion_contract_ref: str | None = None
    delegated_budget: int = 0
    budget: SubAgentBudget = field(default_factory=SubAgentBudget)

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise ChatContractError("role must be non-empty")
        if not isinstance(self.kind, SubAgentKind):
            try:
                object.__setattr__(self, "kind", SubAgentKind(self.kind))
            except ValueError as error:
                raise ChatContractError(
                    "kind must be 'conversation' or 'one_shot'"
                ) from error
        for name in ("input_contract_ref", "completion_contract_ref"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ChatContractError(f"{name} must be non-empty when set")
        tools = tuple(
            dict.fromkeys(
                tool for tool in self.allowed_tools if isinstance(tool, str) and tool
            )
        )
        if len(tools) != len(self.allowed_tools):
            raise ChatContractError("allowed_tools must be unique non-empty names")
        object.__setattr__(self, "allowed_tools", tools)
        if self.delegated_budget < 0:
            raise ChatContractError("delegated_budget must not be negative")


__all__ = [
    "DEFAULT_CONSTRUCTION_ATTEMPTS",
    "DEFAULT_MAX_CHILDREN",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_REPAIR_TURNS",
    "SubAgentBudget",
    "SubAgentSpec",
]
