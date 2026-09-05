"""Deterministic bounded search policy for autonomous strategy discovery.

The policy owns the three controls that keep autonomous discovery auditable:

- an explicit :class:`DiscoveryBudget` whose guards return decisions instead
  of raising, so budget exhaustion is an explicit workflow state rather than
  a hidden error path;
- deterministic candidate ranking with a cheap-to-expensive evaluation
  ordering and a total, reproducible tie-break;
- deterministic per-candidate seed derivation with no unseeded randomness.

Every candidate carries an explicit :class:`SearchRationale` record so
generated, ranked, rejected, aborted, and implemented attempts can be audited
without reconstructing model intent.  The policy itself is stateless: callers
hold the :class:`BudgetUsage` snapshot, which keeps re-runs and pauses
reproducible.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_STATEMENT_LIMIT = 4_096
_COST_MIN = 1
_COST_MAX = 9

BudgetKind = Literal["candidate", "model_invocation", "backtest"]

_SPEND: Mapping[str, tuple[str, str, str]] = MappingProxyType(
    {
        "candidate": ("candidates_generated", "max_candidates", "candidate"),
        "model_invocation": (
            "model_invocations",
            "max_model_invocations",
            "model invocation",
        ),
        "backtest": ("backtests", "max_backtests", "backtest"),
    }
)


class SearchPolicyError(ValueError):
    """Raised when a caller violates the bounded-search invariants."""


class RationaleKind(StrEnum):
    """The auditable kinds of research rationale a candidate can carry."""

    ECONOMIC_PREMISE = "economic-premise"
    NOVELTY = "novelty"
    RANKING = "ranking"
    REJECTION = "rejection"
    ABORT = "abort"
    IMPLEMENTATION = "implementation"


class CandidateStatus(StrEnum):
    """The explicit lifecycle status of one discovery candidate."""

    GENERATED = "generated"
    RANKED = "ranked"
    REJECTED = "rejected"
    ABORTED = "aborted"
    IMPLEMENTED = "implemented"


def _text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SearchPolicyError(f"{label} must be non-empty")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise SearchPolicyError(f"{label} contains unsupported characters")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise SearchPolicyError(f"{label} must be a safe identifier")
    return value


def _references(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise SearchPolicyError("rationale references must be a sequence of strings")
    for item in values:
        _identifier(item, "rationale reference")
    return tuple(sorted(dict.fromkeys(values)))


@dataclass(frozen=True, slots=True)
class SearchRationale:
    """An explicit, auditable research rationale attached to a decision."""

    kind: RationaleKind
    statement: str
    references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RationaleKind):
            raise SearchPolicyError("rationale kind must be a RationaleKind")
        object.__setattr__(
            self,
            "statement",
            _text(self.statement, "rationale statement", maximum=4_096),
        )
        object.__setattr__(self, "references", _references(self.references))

    def as_dict(self) -> dict[str, object]:
        """Return the portable audit representation of this rationale."""
        return {
            "kind": self.kind.value,
            "references": list(self.references),
            "statement": self.statement,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """One generated discovery candidate with its research rationale."""

    candidate_id: str
    hypothesis: str
    market: str
    horizon: str
    direction: str
    rationale: SearchRationale
    cost: int = 3
    source: Mapping[str, str] | None = None
    tests: Mapping[str, str] | None = None
    harness_configuration: Mapping[str, object] = field(default_factory=dict)
    model_invocation_id: str | None = None
    rank: int | None = None
    status: CandidateStatus = CandidateStatus.GENERATED

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        object.__setattr__(
            self, "hypothesis", _text(self.hypothesis, "hypothesis", maximum=8_192)
        )
        _identifier(self.market, "market")
        _identifier(self.horizon, "horizon")
        _identifier(self.direction, "direction")
        if not isinstance(self.rationale, SearchRationale):
            raise SearchPolicyError("candidate rationale must be a SearchRationale")
        if not isinstance(self.cost, int) or isinstance(self.cost, bool):
            raise SearchPolicyError("cost must be an integer")
        if not _COST_MIN <= self.cost <= _COST_MAX:
            raise SearchPolicyError(f"cost must be between {_COST_MIN} and {_COST_MAX}")
        for name in ("source", "tests"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, Mapping) or not value:
                raise SearchPolicyError(f"{name} must be a non-empty mapping")
            for path, content in value.items():
                if not isinstance(path, str) or not path.strip():
                    raise SearchPolicyError(f"{name} paths must be non-empty")
                if not isinstance(content, str):
                    raise SearchPolicyError(f"{name} values must be path-to-text")
            object.__setattr__(self, name, MappingProxyType(dict(value)))
        if not isinstance(self.harness_configuration, Mapping):
            raise SearchPolicyError("harness_configuration must be a mapping")
        object.__setattr__(
            self,
            "harness_configuration",
            MappingProxyType(dict(self.harness_configuration)),
        )
        if self.model_invocation_id is not None:
            _identifier(self.model_invocation_id, "model_invocation_id")
        if self.rank is not None and (not isinstance(self.rank, int) or self.rank < 1):
            raise SearchPolicyError("rank must be a positive integer when set")
        if not isinstance(self.status, CandidateStatus):
            raise SearchPolicyError("status must be a CandidateStatus")

    def as_dict(self) -> dict[str, object]:
        """Return the portable candidate representation without file content."""
        return {
            "candidateId": self.candidate_id,
            "cost": self.cost,
            "direction": self.direction,
            "harnessConfiguration": dict(self.harness_configuration),
            "hasSource": self.source is not None,
            "hasTests": self.tests is not None,
            "hypothesis": self.hypothesis,
            "horizon": self.horizon,
            "market": self.market,
            "modelInvocationId": self.model_invocation_id,
            "rank": self.rank,
            "rationale": self.rationale.as_dict(),
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryBudget:
    """The explicit candidate, model-invocation, and backtest budget."""

    max_candidates: int
    max_model_invocations: int
    max_backtests: int

    def __post_init__(self) -> None:
        for name in ("max_candidates", "max_model_invocations"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise SearchPolicyError(f"{name} must be a positive integer")
        if (
            not isinstance(self.max_backtests, int)
            or isinstance(self.max_backtests, bool)
            or self.max_backtests < 0
        ):
            raise SearchPolicyError("max_backtests must be a non-negative integer")

    def as_dict(self) -> dict[str, int]:
        """Return the portable budget representation."""
        return {
            "maxBacktests": self.max_backtests,
            "maxCandidates": self.max_candidates,
            "maxModelInvocations": self.max_model_invocations,
        }


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """The immutable spend snapshot held by the caller between decisions."""

    candidates_generated: int = 0
    model_invocations: int = 0
    backtests: int = 0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SearchPolicyError(f"{name} must be a non-negative integer")

    def consumed(self, kind: BudgetKind) -> BudgetUsage:
        """Return the spend snapshot after consuming one unit of ``kind``."""
        field_name, _, _ = _SPEND[kind]
        return replace(self, **{field_name: getattr(self, field_name) + 1})

    def as_dict(self) -> dict[str, int]:
        """Return the portable spend representation."""
        return {
            "backtests": self.backtests,
            "candidatesGenerated": self.candidates_generated,
            "modelInvocations": self.model_invocations,
        }


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """An explicit grant or denial for consuming one budgeted unit."""

    kind: BudgetKind
    granted: bool
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "reason", _text(self.reason, "decision reason", maximum=512)
        )

    def as_dict(self) -> dict[str, object]:
        """Return the portable decision representation."""
        return {"granted": self.granted, "kind": self.kind, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class SearchPlan:
    """The deterministic cheap-to-expensive evaluation plan."""

    ordered: tuple[DiscoveryCandidate, ...] = ()
    duplicates: tuple[DiscoveryCandidate, ...] = ()


class SearchPolicy:
    """Rank candidates, derive seeds, and guard budgets deterministically."""

    def __init__(self, budget: DiscoveryBudget, *, seed: int = 0) -> None:
        if not isinstance(budget, DiscoveryBudget):
            raise SearchPolicyError("budget must be a DiscoveryBudget")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise SearchPolicyError("seed must be a non-negative integer")
        self._budget = budget
        self._seed = seed

    @property
    def budget(self) -> DiscoveryBudget:
        """Return the configured discovery budget."""
        return self._budget

    @property
    def seed(self) -> int:
        """Return the run seed used for deterministic candidate derivation."""
        return self._seed

    def authorize(self, kind: BudgetKind, usage: BudgetUsage) -> BudgetDecision:
        """Return an explicit decision for consuming one unit of ``kind``."""
        if not isinstance(usage, BudgetUsage):
            raise SearchPolicyError("usage must be a BudgetUsage")
        field_name, budget_name, label = _SPEND[kind]
        used = getattr(usage, field_name)
        limit = getattr(self._budget, budget_name)
        if used >= limit:
            return BudgetDecision(
                kind=kind,
                granted=False,
                reason=f"{label} budget exhausted: {used} of {limit} used",
            )
        return BudgetDecision(
            kind=kind,
            granted=True,
            reason=f"{label} {used + 1} of {limit} granted",
        )

    def rank(self, candidates: Sequence[DiscoveryCandidate]) -> SearchPlan:
        """Rank admitted candidates cheap-to-expensive with a total tie-break.

        The ordering key is ``(cost, candidate_id)``: cheaper candidates are
        evaluated first and the identifier tie-break makes the order total
        and reproducible.  Later duplicates of an already admitted candidate
        are returned separately so the caller can record them explicitly.
        """
        if len(candidates) > self._budget.max_candidates:
            raise SearchPolicyError(
                "candidate count exceeds the discovery budget: "
                f"{len(candidates)} of {self._budget.max_candidates}"
            )
        admitted: dict[str, DiscoveryCandidate] = {}
        duplicates: list[DiscoveryCandidate] = []
        for candidate in candidates:
            if candidate.candidate_id in admitted:
                duplicates.append(candidate)
            else:
                admitted[candidate.candidate_id] = candidate
        ordered_ids = sorted(admitted, key=lambda cid: (admitted[cid].cost, cid))
        ordered = tuple(
            replace(admitted[cid], status=CandidateStatus.RANKED, rank=position)
            for position, cid in enumerate(ordered_ids, start=1)
        )
        return SearchPlan(ordered=ordered, duplicates=tuple(duplicates))

    def candidate_seed(self, candidate_id: str) -> int:
        """Derive a deterministic per-candidate seed from the run seed."""
        _identifier(candidate_id, "candidate_id")
        digest = hashlib.blake2b(
            f"{self._seed}:{candidate_id}".encode(), digest_size=8
        ).digest()
        return int.from_bytes(digest, "big")


__all__ = [
    "BudgetDecision",
    "BudgetKind",
    "BudgetUsage",
    "CandidateStatus",
    "DiscoveryBudget",
    "DiscoveryCandidate",
    "RationaleKind",
    "SearchPlan",
    "SearchPolicy",
    "SearchPolicyError",
    "SearchRationale",
]
