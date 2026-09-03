"""Deterministic records shared by the synchronous risk engine and rules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.common import DecimalInput, exact_decimal, text
from harness.core.types.orders import OrderRequest
from harness.portfolio.hub import PortfolioSnapshot


class RiskAction(StrEnum):
    """The three outcomes a risk rule or pipeline can produce."""

    ALLOW = "allow"
    REDUCE = "reduce"
    REJECT = "reject"


DecisionAction = RiskAction


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Explicit point-in-time inputs supplied to a risk evaluation.

    The risk kernel never discovers prices, time, or account state itself.  A
    caller supplies market prices for unmarked symbols and the current daily
    loss/order count as part of this immutable context.
    """

    portfolio: PortfolioSnapshot
    prices: Mapping[str | Symbol, Decimal] = field(default_factory=dict)
    daily_loss: DecimalInput = Decimal("0")
    order_count: int = 0
    strategy_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.portfolio, PortfolioSnapshot):
            raise TypeError("portfolio must be a PortfolioSnapshot")
        if not isinstance(self.prices, Mapping):
            raise TypeError("prices must be a mapping")
        normalized: dict[Symbol, Decimal] = {}
        for symbol, price in self.prices.items():
            normalized[canonical_symbol(symbol)] = exact_decimal(price, "price")
            if normalized[canonical_symbol(symbol)] <= 0:
                raise ValueError("price must be positive")
        object.__setattr__(self, "prices", MappingProxyType(normalized))
        object.__setattr__(
            self, "daily_loss", exact_decimal(self.daily_loss, "daily_loss")
        )
        if (
            isinstance(self.order_count, bool)
            or not isinstance(self.order_count, int)
            or self.order_count < 0
        ):
            raise ValueError("order_count must be a non-negative integer")
        if self.strategy_id is not None:
            object.__setattr__(
                self, "strategy_id", text(self.strategy_id, "strategy_id")
            )

    @property
    def market_prices(self) -> Mapping[str | Symbol, Decimal]:
        """Alias for the explicitly supplied price map."""
        return self.prices

    @property
    def pending_order_count(self) -> int:
        """Alias used by callers that count currently pending orders."""
        return self.order_count

    def price(self, symbol: str | Symbol) -> Decimal | None:
        """Return an explicitly supplied price for ``symbol`` if present."""
        return self.prices.get(canonical_symbol(symbol))


RiskState = RiskContext


@dataclass(frozen=True, slots=True)
class RuleDecision:
    """One rule's immutable result for one candidate order."""

    action: RiskAction
    order: OrderRequest
    reason: str | None = None
    metrics: Mapping[str, object] = field(default_factory=dict)
    halt: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.action, RiskAction):
            object.__setattr__(self, "action", RiskAction(self.action))
        if not isinstance(self.metrics, Mapping):
            raise TypeError("metrics must be a mapping")
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        if self.reason is not None:
            object.__setattr__(self, "reason", text(self.reason, "reason"))

    @classmethod
    def allow(
        cls, order: OrderRequest, *, metrics: Mapping[str, object] | None = None
    ) -> RuleDecision:
        """Build an allow result without a reason."""
        return cls(RiskAction.ALLOW, order, metrics={} if metrics is None else metrics)

    @classmethod
    def reduce(
        cls,
        order: OrderRequest,
        reason: str,
        *,
        metrics: Mapping[str, object] | None = None,
    ) -> RuleDecision:
        """Build a reduction result."""
        return cls(
            RiskAction.REDUCE,
            order,
            reason,
            {} if metrics is None else metrics,
        )

    @classmethod
    def reject(
        cls,
        order: OrderRequest,
        reason: str,
        *,
        metrics: Mapping[str, object] | None = None,
        halt: bool = False,
    ) -> RuleDecision:
        """Build a rejection result, optionally marking a halt condition."""
        return cls(
            RiskAction.REJECT,
            order,
            reason,
            {} if metrics is None else metrics,
            halt,
        )


RuleResult = RuleDecision


@dataclass(frozen=True, slots=True)
class RiskAudit:
    """Complete audit record for one ordered rule invocation."""

    sequence: int
    pass_number: int
    rule_id: str
    action: RiskAction
    order_before: OrderRequest
    order_after: OrderRequest
    reason: str | None
    metrics: Mapping[str, object]
    recheck: bool
    halt: bool = False

    def __post_init__(self) -> None:
        if self.sequence < 0 or self.pass_number < 1:
            raise ValueError("audit sequence and pass_number must be positive")
        object.__setattr__(self, "rule_id", text(self.rule_id, "rule_id"))
        if not isinstance(self.action, RiskAction):
            object.__setattr__(self, "action", RiskAction(self.action))
        if not isinstance(self.metrics, Mapping):
            raise TypeError("metrics must be a mapping")
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    @property
    def decision(self) -> RiskAction:
        """Readable alias for the rule action."""
        return self.action


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Final pipeline decision plus every rule invocation that produced it."""

    action: RiskAction
    original_order: OrderRequest
    order: OrderRequest
    audits: tuple[RiskAudit, ...]
    reason: str | None = None
    reasons: tuple[str, ...] = ()
    strategy_id: str | None = None
    halt_scope: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, RiskAction):
            object.__setattr__(self, "action", RiskAction(self.action))
        object.__setattr__(self, "audits", tuple(self.audits))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        if self.reason is not None:
            object.__setattr__(self, "reason", text(self.reason, "reason"))
        if self.strategy_id is not None:
            object.__setattr__(
                self, "strategy_id", text(self.strategy_id, "strategy_id")
            )

    @property
    def final_order(self) -> OrderRequest:
        """Return the order that would be submitted when approved."""
        return self.order

    @property
    def requested_order(self) -> OrderRequest:
        """Return the unmodified order supplied by the caller."""
        return self.original_order

    @property
    def audit(self) -> tuple[RiskAudit, ...]:
        """Singular alias retained for concise audit consumers."""
        return self.audits

    @property
    def audit_trail(self) -> tuple[RiskAudit, ...]:
        """Return the ordered complete audit trail."""
        return self.audits

    @property
    def veto_reasons(self) -> tuple[str, ...]:
        """Return all rejection reasons, including a recheck veto."""
        return self.reasons

    @property
    def approved(self) -> bool:
        """Whether the final order may be submitted."""
        return self.action is not RiskAction.REJECT

    @property
    def allowed(self) -> bool:
        """Alias for :attr:`approved`."""
        return self.approved

    @property
    def rejected(self) -> bool:
        """Whether the final order was vetoed."""
        return self.action is RiskAction.REJECT

    @property
    def reduced(self) -> bool:
        """Whether the approved order differs from the request."""
        return self.action is RiskAction.REDUCE or self.order != self.original_order

    @property
    def halted(self) -> bool:
        """Whether this decision requests a strategy/runtime halt."""
        return self.halt_scope is not None or any(a.halt for a in self.audits)


Decision = RiskDecision


@runtime_checkable
class RiskRule(Protocol):
    """Structural contract implemented by synchronous risk rules."""

    rule_id: str

    def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
        """Evaluate one candidate order against one explicit context."""
        ...


__all__ = [
    "Decision",
    "DecisionAction",
    "RiskAction",
    "RiskAudit",
    "RiskContext",
    "RiskDecision",
    "RiskRule",
    "RiskState",
    "RuleDecision",
    "RuleResult",
]
