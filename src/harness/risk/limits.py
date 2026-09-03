"""Exact-decimal built-in pre-trade risk rules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal

from harness.core.symbols import Symbol
from harness.core.types.common import exact_decimal, text
from harness.core.types.orders import OrderRequest, Side, SingleLegOrder
from harness.portfolio.hub import AggregatePosition, PortfolioSnapshot
from harness.risk.models import RiskContext, RuleDecision

_ZERO = Decimal("0")
_ONE = Decimal("1")


def _value(
    parameters: Mapping[str, object], name: str, default: object = None
) -> object:
    value = parameters.get(name, default)
    if value is None:
        raise ValueError(f"risk rule parameter {name!r} is required")
    return value


def _named_decimal_parameter(
    parameters: Mapping[str, object], names: tuple[str, ...]
) -> Decimal:
    for name in names:
        if name in parameters:
            value = exact_decimal(parameters[name], name)
            if value < _ZERO:
                raise ValueError(f"risk rule parameter {name!r} must be non-negative")
            return value
    raise ValueError(f"risk rule parameter {names[0]!r} is required")


def _merge_strategy_parameters(
    parameters: Mapping[str, object], context: RiskContext
) -> dict[str, object]:
    """Apply the selected strategy override without mutating configuration."""
    merged = dict(parameters)
    if context.strategy_id is None:
        return merged
    for key in (
        "overrides",
        "per_strategy",
        "strategy_overrides",
        "strategies",
        "strategy_limits",
        "by_strategy",
    ):
        overrides = parameters.get(key)
        if isinstance(overrides, Mapping):
            selected = overrides.get(context.strategy_id)
            if isinstance(selected, Mapping):
                merged.update(selected)
            elif selected is not None:
                merged["limit"] = selected
            break
    return merged


class ConfiguredRiskRule:
    """Base class accepting either a parameter mapping or a direct limit."""

    rule_id: str

    def __init__(
        self,
        parameters: Mapping[str, object] | object | None = None,
        *,
        rule_id: str | None = None,
        **values: object,
    ) -> None:
        if parameters is None:
            configured: dict[str, object] = {}
        elif isinstance(parameters, Mapping):
            configured = dict(parameters)
        else:
            configured = {"limit": parameters}
        configured.update(values)
        self._parameters = configured
        self.rule_id = text(rule_id or type(self).__name__, "rule_id")

    def parameters_for(self, context: RiskContext) -> dict[str, object]:
        """Return this rule's base parameters with the strategy override applied."""
        return _merge_strategy_parameters(self._parameters, context)


def order_price(
    order: OrderRequest, context: RiskContext, symbol: Symbol
) -> Decimal | None:
    """Return a deterministic risk price for one order symbol."""
    if isinstance(order, SingleLegOrder):
        if order.limit_price is not None:
            return order.limit_price
        if order.stop_price is not None:
            return order.stop_price
    explicit = context.price(symbol)
    if explicit is not None:
        return explicit
    position = _position(context.portfolio, symbol)
    return None if position is None else position.mark_price


def order_notional(order: OrderRequest, context: RiskContext) -> Decimal | None:
    """Return gross order notional, or ``None`` when a price is unavailable."""
    if isinstance(order, SingleLegOrder):
        price = order_price(order, context, order.symbol)
        return None if price is None else order.quantity * price
    total = _ZERO
    for leg in order.legs:
        price = order_price(order, context, leg.symbol)
        if price is None:
            return None
        total += leg.quantity * price
    return total


def order_size(order: OrderRequest) -> Decimal:
    """Return one exact size value for single- and multi-leg orders."""
    if isinstance(order, SingleLegOrder):
        return order.quantity
    return sum((leg.quantity for leg in order.legs), start=_ZERO)


def scale_order(order: OrderRequest, ratio: Decimal) -> OrderRequest:
    """Scale every order leg by an exact positive ratio."""
    if not _ZERO < ratio <= _ONE:
        raise ValueError("order reduction ratio must be greater than zero")
    if isinstance(order, SingleLegOrder):
        return replace(order, quantity=order.quantity * ratio)
    return replace(
        order,
        legs=tuple(replace(leg, quantity=leg.quantity * ratio) for leg in order.legs),
    )


def _position(snapshot: PortfolioSnapshot, symbol: Symbol) -> AggregatePosition | None:
    try:
        return snapshot.position(symbol)
    except KeyError:
        return None


def _position_price(
    position: AggregatePosition | None, symbol: Symbol, context: RiskContext
) -> Decimal | None:
    explicit = context.price(symbol)
    if explicit is not None:
        return explicit
    return None if position is None else position.mark_price


def _order_legs(
    order: OrderRequest,
) -> tuple[tuple[Symbol, Side, Decimal], ...]:
    if isinstance(order, SingleLegOrder):
        return ((order.symbol, order.side, order.quantity),)
    return tuple((leg.symbol, leg.side, leg.quantity) for leg in order.legs)


def _exposure_components(
    order: OrderRequest, context: RiskContext
) -> tuple[
    dict[Symbol, Decimal],
    dict[Symbol, Decimal],
    dict[Symbol, Decimal],
    dict[Symbol, Decimal],
    dict[Symbol, Decimal] | None,
]:
    """Return current quantities, order deltas, and prices by affected symbol."""
    current = {
        position.symbol: position.quantity for position in context.portfolio.positions
    }
    long = {
        position.symbol: position.long_quantity
        for position in context.portfolio.positions
    }
    short = {
        position.symbol: position.short_quantity
        for position in context.portfolio.positions
    }
    deltas: dict[Symbol, Decimal] = {}
    prices: dict[Symbol, Decimal] = {}
    for symbol, side, quantity in _order_legs(order):
        position = _position(context.portfolio, symbol)
        price = _position_price(position, symbol, context)
        if price is None:
            return current, deltas, long, short, None
        prices[symbol] = price
        signed = quantity if side is Side.BUY else -quantity
        deltas[symbol] = deltas.get(symbol, _ZERO) + signed
    for position in context.portfolio.positions:
        if position.symbol in prices:
            continue
        price = _position_price(position, position.symbol, context)
        if price is None:
            return current, deltas, long, short, None
        prices[position.symbol] = price
    return current, deltas, long, short, prices


def _gross_quantity(
    long_quantity: Decimal, short_quantity: Decimal, delta: Decimal
) -> Decimal:
    """Return long plus short quantity after one signed position delta."""
    if delta >= _ZERO:
        return (
            long_quantity
            + max(delta - short_quantity, _ZERO)
            + max(short_quantity - delta, _ZERO)
        )
    return (
        max(long_quantity + delta, _ZERO)
        + short_quantity
        + max(-delta - long_quantity, _ZERO)
    )


def _projected_gross(order: OrderRequest, context: RiskContext) -> Decimal | None:
    """Calculate gross position value after applying the candidate order."""
    _, deltas, long, short, prices = _exposure_components(order, context)
    if prices is None:
        return None
    return sum(
        (
            _gross_quantity(long.get(symbol, _ZERO), short.get(symbol, _ZERO), delta)
            * price
            for symbol, price in prices.items()
            for delta in (deltas.get(symbol, _ZERO),)
        ),
        start=_ZERO,
    )


def _projected_net(order: OrderRequest, context: RiskContext) -> Decimal | None:
    current, deltas, _, _, prices = _exposure_components(order, context)
    if prices is None:
        return None
    return abs(
        sum(
            (
                (current.get(symbol, _ZERO) + deltas.get(symbol, _ZERO)) * price
                for symbol, price in prices.items()
            ),
            start=_ZERO,
        )
    )


def _projected_position(order: OrderRequest, context: RiskContext) -> Decimal | None:
    current, deltas, _, _, prices = _exposure_components(order, context)
    if prices is None:
        return None
    return max(
        (
            abs(current.get(symbol, _ZERO) + deltas.get(symbol, _ZERO)) * prices[symbol]
            for symbol in deltas
        ),
        default=_ZERO,
    )


def _reduced_by_notional(
    order: OrderRequest, capacity: Decimal, context: RiskContext
) -> OrderRequest | None:
    current = order_notional(order, context)
    if current is None or capacity <= _ZERO:
        return None
    if capacity >= current:
        return order
    return scale_order(order, capacity / current)


def _max_valid_scale(
    order: OrderRequest,
    capacity: Decimal,
    context: RiskContext,
    exposure: str,
) -> Decimal | None:
    """Find the greatest exact quantity scale satisfying a cap."""
    if capacity < _ZERO:
        return None
    current, deltas, long, short, prices = _exposure_components(order, context)
    if prices is None:
        return None
    price_map = prices

    def value(scale: Decimal) -> Decimal:
        signed = {
            symbol: current.get(symbol, _ZERO) + deltas.get(symbol, _ZERO) * scale
            for symbol in price_map
        }
        if exposure == "gross":
            return sum(
                (
                    _gross_quantity(
                        long.get(symbol, _ZERO),
                        short.get(symbol, _ZERO),
                        deltas.get(symbol, _ZERO) * scale,
                    )
                    * price_map[symbol]
                    for symbol in price_map
                ),
                start=_ZERO,
            )
        if exposure == "net":
            return abs(
                sum(
                    (
                        quantity * price_map[symbol]
                        for symbol, quantity in signed.items()
                    ),
                    start=_ZERO,
                )
            )
        if exposure == "position":
            return max(
                (abs(signed[symbol]) * price_map[symbol] for symbol in deltas),
                default=_ZERO,
            )
        return _ZERO

    if value(_ONE) <= capacity:
        return _ONE
    points = {_ZERO, _ONE}
    for symbol, delta in deltas.items():
        if delta != _ZERO:
            if exposure == "gross":
                crossing = (
                    -short.get(symbol, _ZERO) / delta
                    if delta > _ZERO
                    else long.get(symbol, _ZERO) / -delta
                )
            else:
                crossing = -current.get(symbol, _ZERO) / delta
            if _ZERO < crossing < _ONE:
                points.add(crossing)
    if exposure == "net":
        current_value = sum(
            (current.get(symbol, _ZERO) * price_map[symbol] for symbol in price_map),
            start=_ZERO,
        )
        delta_value = sum(
            (deltas.get(symbol, _ZERO) * price_map[symbol] for symbol in price_map),
            start=_ZERO,
        )
        if delta_value != _ZERO:
            crossing = -current_value / delta_value
            if _ZERO < crossing < _ONE:
                points.add(crossing)

    best = _ZERO
    ordered_points = tuple(sorted(points))
    for left, right in zip(ordered_points, ordered_points[1:], strict=False):
        left_value = value(left)
        right_value = value(right)
        if right_value <= capacity:
            best = max(best, right)
            continue
        if left_value > capacity:
            continue
        slope = (right_value - left_value) / (right - left)
        if slope != _ZERO:
            crossing = left + (capacity - left_value) / slope
            if left < crossing < right and value(crossing) <= capacity:
                best = max(best, crossing)
    return best if best > _ZERO else None


class MaxOrderNotional(ConfiguredRiskRule):
    """Cap the gross notional of one candidate order."""

    def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
        parameters = self.parameters_for(context)
        limit = _named_decimal_parameter(
            parameters, ("limit", "max_notional", "max_order_notional")
        )
        notional = order_notional(order, context)
        if notional is None:
            return RuleDecision.reject(order, "max_order_notional missing price")
        metrics = {"notional": notional, "limit": limit}
        if notional <= limit:
            return RuleDecision.allow(order, metrics=metrics)
        reduced = _reduced_by_notional(order, limit, context)
        if reduced is None:
            return RuleDecision.reject(
                order, "max_order_notional has no permitted quantity", metrics=metrics
            )
        return RuleDecision.reduce(
            reduced, "max_order_notional reduced order", metrics=metrics
        )


class MaxPositionNotional(ConfiguredRiskRule):
    """Cap the resulting notional of each changed position."""

    def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
        parameters = self.parameters_for(context)
        limit = _named_decimal_parameter(
            parameters, ("limit", "max_notional", "max_position_notional")
        )
        max_position = _projected_position(order, context)
        if max_position is None:
            return RuleDecision.reject(order, "max_position_notional missing price")
        metrics = {"position_notional": max_position, "limit": limit}
        if max_position <= limit:
            return RuleDecision.allow(order, metrics=metrics)

        reduced_scale = _max_valid_scale(order, limit, context, "position")
        reduced = None if reduced_scale is None else scale_order(order, reduced_scale)
        if reduced is None:
            return RuleDecision.reject(
                order,
                "max_position_notional has no permitted quantity",
                metrics=metrics,
            )
        return RuleDecision.reduce(
            reduced, "max_position_notional reduced order", metrics=metrics
        )


class MaxGrossExposure(ConfiguredRiskRule):
    """Cap gross marked exposure after the candidate order."""

    def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
        limit = _named_decimal_parameter(
            self.parameters_for(context),
            ("limit", "max_exposure", "max_gross_exposure"),
        )
        projected = _projected_gross(order, context)
        if projected is None:
            return RuleDecision.reject(order, "max_gross_exposure missing price")
        metrics = {"gross_exposure": projected, "limit": limit}
        if projected <= limit:
            return RuleDecision.allow(order, metrics=metrics)
        reduced_scale = _max_valid_scale(order, limit, context, "gross")
        reduced = None if reduced_scale is None else scale_order(order, reduced_scale)
        if reduced is None:
            return RuleDecision.reject(
                order, "max_gross_exposure has no permitted quantity", metrics=metrics
            )
        return RuleDecision.reduce(
            reduced, "max_gross_exposure reduced order", metrics=metrics
        )


class MaxNetExposure(ConfiguredRiskRule):
    """Cap absolute marked net exposure after the candidate order."""

    def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
        limit = _named_decimal_parameter(
            self.parameters_for(context),
            ("limit", "max_exposure", "max_net_exposure"),
        )
        projected = _projected_net(order, context)
        if projected is None:
            return RuleDecision.reject(order, "max_net_exposure missing price")
        metrics = {"net_exposure": projected, "limit": limit}
        if projected <= limit:
            return RuleDecision.allow(order, metrics=metrics)
        reduced_scale = _max_valid_scale(order, limit, context, "net")
        reduced = None if reduced_scale is None else scale_order(order, reduced_scale)
        if reduced is None:
            return RuleDecision.reject(
                order, "max_net_exposure has no permitted quantity", metrics=metrics
            )
        return RuleDecision.reduce(
            reduced, "max_net_exposure reduced order", metrics=metrics
        )


class MaxOrderSize(ConfiguredRiskRule):
    """Cap the quantity of a single- or multi-leg order."""

    def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
        limit = _named_decimal_parameter(
            self.parameters_for(context),
            ("limit", "max_size", "max_quantity", "max_order_size"),
        )
        size = order_size(order)
        metrics = {"order_size": size, "limit": limit}
        if size <= limit:
            return RuleDecision.allow(order, metrics=metrics)
        reduced = scale_order(order, limit / size) if limit > _ZERO else None
        if reduced is None:
            return RuleDecision.reject(
                order, "max_order_size has no permitted quantity", metrics=metrics
            )
        return RuleDecision.reduce(
            reduced, "max_order_size reduced order", metrics=metrics
        )


class MaxOrderCount(ConfiguredRiskRule):
    """Reject a candidate when the explicit order count is exhausted."""

    def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
        limit = _named_decimal_parameter(
            self.parameters_for(context),
            ("limit", "max_orders", "max_count", "max_order_count"),
        )
        if limit != limit.to_integral_value():
            raise ValueError("max_order_count limit must be an integer")
        count = Decimal(context.order_count)
        metrics = {"order_count": context.order_count, "limit": int(limit)}
        if count < limit:
            return RuleDecision.allow(order, metrics=metrics)
        return RuleDecision.reject(
            order, "max_order_count limit reached", metrics=metrics
        )


class DailyLossHalt(ConfiguredRiskRule):
    """Reject and mark a halt when explicit daily loss reaches its limit."""

    def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
        limit = _named_decimal_parameter(
            self.parameters_for(context),
            ("limit", "max_loss", "daily_loss_limit", "max_daily_loss"),
        )
        loss = max(exact_decimal(context.daily_loss, "daily_loss"), _ZERO)
        metrics = {"daily_loss": loss, "limit": limit}
        if loss < limit:
            return RuleDecision.allow(order, metrics=metrics)
        return RuleDecision.reject(
            order,
            "daily_loss_halt limit reached",
            metrics=metrics,
            halt=True,
        )


# Names commonly used by configuration authors and downstream callers.
GrossExposureLimit = MaxGrossExposure
NetExposureLimit = MaxNetExposure
OrderSizeLimit = MaxOrderSize
OrderCountLimit = MaxOrderCount
DailyLossLimit = DailyLossHalt
DailyLossRule = DailyLossHalt
MaxDailyLoss = DailyLossHalt
GrossExposureRule = MaxGrossExposure
NetExposureRule = MaxNetExposure
OrderCountRule = MaxOrderCount
OrderSizeRule = MaxOrderSize
PositionNotionalLimit = MaxPositionNotional
MaxOrders = MaxOrderCount
MaxQuantity = MaxOrderSize


__all__ = [
    "ConfiguredRiskRule",
    "DailyLossHalt",
    "DailyLossLimit",
    "DailyLossRule",
    "GrossExposureLimit",
    "GrossExposureRule",
    "MaxGrossExposure",
    "MaxDailyLoss",
    "MaxNetExposure",
    "MaxOrderCount",
    "MaxOrderNotional",
    "MaxOrders",
    "MaxOrderSize",
    "MaxQuantity",
    "MaxPositionNotional",
    "NetExposureLimit",
    "NetExposureRule",
    "OrderCountLimit",
    "OrderCountRule",
    "OrderSizeLimit",
    "OrderSizeRule",
    "PositionNotionalLimit",
    "order_notional",
    "order_price",
    "order_size",
    "scale_order",
]
