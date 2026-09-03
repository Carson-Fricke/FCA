"""Ordered, synchronous YAML-configurable risk evaluation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from importlib import import_module

from harness.configuration.models import RiskPipelineConfig
from harness.core.symbols import Symbol
from harness.core.types.common import DecimalInput, exact_decimal
from harness.core.types.orders import OrderRequest
from harness.portfolio.hub import PortfolioSnapshot
from harness.risk.limits import (
    DailyLossHalt,
    MaxGrossExposure,
    MaxNetExposure,
    MaxOrderCount,
    MaxOrderNotional,
    MaxOrderSize,
    MaxPositionNotional,
)
from harness.risk.models import (
    RiskAction,
    RiskAudit,
    RiskContext,
    RiskDecision,
    RiskRule,
    RuleDecision,
)

_BUILTINS = {
    "DailyLossHalt": DailyLossHalt,
    "DailyLossLimit": DailyLossHalt,
    "DailyLossRule": DailyLossHalt,
    "MaxDailyLoss": DailyLossHalt,
    "MaxGrossExposure": MaxGrossExposure,
    "GrossExposureLimit": MaxGrossExposure,
    "GrossExposureRule": MaxGrossExposure,
    "MaxNetExposure": MaxNetExposure,
    "NetExposureLimit": MaxNetExposure,
    "NetExposureRule": MaxNetExposure,
    "MaxOrderCount": MaxOrderCount,
    "MaxOrders": MaxOrderCount,
    "OrderCountLimit": MaxOrderCount,
    "OrderCountRule": MaxOrderCount,
    "MaxOrderNotional": MaxOrderNotional,
    "MaxOrderSize": MaxOrderSize,
    "MaxQuantity": MaxOrderSize,
    "OrderSizeLimit": MaxOrderSize,
    "OrderSizeRule": MaxOrderSize,
    "MaxPositionNotional": MaxPositionNotional,
    "PositionNotionalLimit": MaxPositionNotional,
}


def _is_order(value: object) -> bool:
    from harness.core.types.orders import MultiLegOrder, SingleLegOrder

    return isinstance(value, (SingleLegOrder, MultiLegOrder))


def _rule_id(rule: RiskRule) -> str:
    value = getattr(rule, "rule_id", type(rule).__name__)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("risk rules require a non-empty rule_id")
    return value.strip()


def _implementation(path: str) -> type[object]:
    """Resolve one configured implementation without touching I/O or a clock."""
    if ":" not in path:
        raise ValueError("risk implementation must use module:Class syntax")
    module_name, class_name = path.rsplit(":", 1)
    if module_name == "harness.risk.limits" and class_name in _BUILTINS:
        return _BUILTINS[class_name]
    module = import_module(module_name)
    implementation = getattr(module, class_name, None)
    if not isinstance(implementation, type):
        raise TypeError(f"risk implementation {path!r} is not a class")
    return implementation


def _configured_rules(configuration: RiskPipelineConfig) -> tuple[RiskRule, ...]:
    rules: list[RiskRule] = []
    for configured in configuration.rules:
        implementation = _implementation(configured.implementation)
        try:
            instance = implementation(configured.parameters, rule_id=configured.rule_id)
        except TypeError as error:
            raise TypeError(
                f"risk implementation {configured.implementation!r} must accept "
                "parameters and rule_id"
            ) from error
        if not isinstance(instance, RiskRule):
            raise TypeError(
                f"risk implementation {configured.implementation!r} is not a risk rule"
            )
        rules.append(instance)
    return tuple(rules)


class RiskPipeline:
    """Evaluate orders through rules in stable configured order.

    A reduction restarts at the first rule.  This makes every earlier rule
    re-check the changed order and records both the reduction and all rechecks.
    The pipeline has no implicit state, time source, randomness, or I/O.
    """

    __slots__ = ("_on_reject", "_rules")

    def __init__(
        self,
        rules: RiskPipelineConfig | Sequence[RiskRule] | Iterable[RiskRule],
        *,
        on_reject: str = "reject_order",
    ) -> None:
        if isinstance(rules, RiskPipelineConfig):
            on_reject = rules.on_reject
            normalized = _configured_rules(rules)
        else:
            normalized = tuple(rules)
        if not normalized:
            raise ValueError("risk pipeline requires at least one rule")
        if on_reject not in {"reject_order", "halt_strategy", "halt_runtime"}:
            raise ValueError(
                "on_reject must be reject_order, halt_strategy, or halt_runtime"
            )
        for rule in normalized:
            if not isinstance(rule, RiskRule):
                raise TypeError("risk pipeline rules must implement RiskRule")
            _rule_id(rule)
        self._rules = normalized
        self._on_reject = on_reject

    @classmethod
    def from_config(cls, configuration: RiskPipelineConfig) -> RiskPipeline:
        """Build a pipeline from the existing strict YAML configuration model."""
        if not isinstance(configuration, RiskPipelineConfig):
            raise TypeError("configuration must be a RiskPipelineConfig")
        return cls(configuration)

    @property
    def rules(self) -> tuple[RiskRule, ...]:
        """Return configured rules in evaluation order."""
        return self._rules

    @property
    def on_reject(self) -> str:
        """Return the configured rejection policy."""
        return self._on_reject

    def evaluate(
        self,
        order: OrderRequest,
        context: RiskContext | PortfolioSnapshot,
        *,
        prices: Mapping[str | Symbol, Decimal] | None = None,
        daily_loss: DecimalInput | None = None,
        order_count: int | None = None,
        strategy_id: str | None = None,
    ) -> RiskDecision:
        """Return a complete deterministic decision for one candidate order.

        A :class:`RiskContext` is preferred.  Passing a portfolio snapshot is a
        convenience for callers already holding the core portfolio concept;
        the other keyword inputs then complete its explicit context.
        """
        if not _is_order(order):
            raise TypeError("order must be a SingleLegOrder or MultiLegOrder")
        state = self._context(
            context,
            prices=prices,
            daily_loss=daily_loss,
            order_count=order_count,
            strategy_id=strategy_id,
        )
        original = order
        current = order
        audits: list[RiskAudit] = []
        reasons: list[str] = []
        pass_number = 1
        index = 0
        while index < len(self._rules):
            rule = self._rules[index]
            before = current
            current_recheck = pass_number > 1
            result = rule.evaluate(current, state)
            if not isinstance(result, RuleDecision):
                raise TypeError(
                    f"risk rule {_rule_id(rule)!r} returned an invalid decision"
                )
            if not _is_order(result.order):
                raise TypeError(
                    f"risk rule {_rule_id(rule)!r} returned an invalid order"
                )
            action = result.action
            after = result.order if action is RiskAction.REDUCE else current
            reason = result.reason
            audits.append(
                RiskAudit(
                    sequence=len(audits),
                    pass_number=pass_number,
                    rule_id=_rule_id(rule),
                    action=action,
                    order_before=before,
                    order_after=after,
                    reason=reason,
                    metrics=result.metrics,
                    recheck=current_recheck,
                    halt=result.halt,
                )
            )
            if reason is not None:
                reasons.append(reason)
            if action is RiskAction.REJECT:
                return self._decision(
                    original,
                    current,
                    audits,
                    reasons,
                    state,
                    reason,
                )
            if action is RiskAction.REDUCE:
                if result.order == current:
                    reasons.append(f"{_rule_id(rule)} returned an unchanged reduction")
                    audits.append(
                        RiskAudit(
                            sequence=len(audits),
                            pass_number=pass_number,
                            rule_id=_rule_id(rule),
                            action=RiskAction.REJECT,
                            order_before=current,
                            order_after=current,
                            reason=reasons[-1],
                            metrics=result.metrics,
                            recheck=current_recheck,
                            halt=False,
                        )
                    )
                    return self._decision(
                        original,
                        current,
                        audits,
                        reasons,
                        state,
                        reasons[-1],
                    )
                current = result.order
                pass_number += 1
                index = 0
                continue
            index += 1
        action = RiskAction.REDUCE if current != original else RiskAction.ALLOW
        return RiskDecision(
            action=action,
            original_order=original,
            order=current,
            audits=tuple(audits),
            reason=reasons[-1] if action is RiskAction.REDUCE and reasons else None,
            reasons=tuple(reasons),
            strategy_id=state.strategy_id,
        )

    def check(
        self,
        order: OrderRequest,
        context: RiskContext | PortfolioSnapshot,
        *,
        prices: Mapping[str | Symbol, Decimal] | None = None,
        daily_loss: DecimalInput | None = None,
        order_count: int | None = None,
        strategy_id: str | None = None,
    ) -> RiskDecision:
        """Alias for :meth:`evaluate` used by order admission callers."""
        return self.evaluate(
            order,
            context,
            prices=prices,
            daily_loss=daily_loss,
            order_count=order_count,
            strategy_id=strategy_id,
        )

    def _context(
        self,
        context: RiskContext | PortfolioSnapshot,
        *,
        prices: Mapping[str | Symbol, Decimal] | None,
        daily_loss: DecimalInput | None,
        order_count: int | None,
        strategy_id: str | None,
    ) -> RiskContext:
        if isinstance(context, RiskContext):
            if (
                any(value is not None for value in (prices, strategy_id))
                or daily_loss is not None
                or order_count is not None
            ):
                return RiskContext(
                    portfolio=context.portfolio,
                    prices=context.prices if prices is None else prices,
                    daily_loss=(
                        context.daily_loss
                        if daily_loss is None
                        else exact_decimal(daily_loss, "daily_loss")
                    ),
                    order_count=context.order_count
                    if order_count is None
                    else order_count,
                    strategy_id=context.strategy_id
                    if strategy_id is None
                    else strategy_id,
                )
            return context
        if not isinstance(context, PortfolioSnapshot):
            raise TypeError("context must be a RiskContext or PortfolioSnapshot")
        return RiskContext(
            portfolio=context,
            prices={} if prices is None else prices,
            daily_loss=Decimal("0")
            if daily_loss is None
            else exact_decimal(daily_loss, "daily_loss"),
            order_count=0 if order_count is None else order_count,
            strategy_id=strategy_id,
        )

    def _decision(
        self,
        original: OrderRequest,
        current: OrderRequest,
        audits: list[RiskAudit],
        reasons: list[str],
        context: RiskContext,
        reason: str | None,
    ) -> RiskDecision:
        return RiskDecision(
            action=RiskAction.REJECT,
            original_order=original,
            order=current,
            audits=tuple(audits),
            reason=reason,
            reasons=tuple(reasons),
            strategy_id=context.strategy_id,
            halt_scope=(self._on_reject if self._on_reject != "reject_order" else None),
        )


RiskEngine = RiskPipeline


__all__ = ["RiskEngine", "RiskPipeline"]
