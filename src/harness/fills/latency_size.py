"""Deterministic latency and displayed-size constrained fill decisions.

The model deliberately keeps response latency as plugin metadata.  The
simulated venue owns response scheduling, while this plugin owns the
execution assumptions that produced a :class:`FillDecision`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final

from harness.configuration.loader import load_yaml_mapping
from harness.core.types.common import exact_decimal
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.core.types.orders import OrderType, Side, SingleLegOrder
from harness.fills.simple_touch import FillDecision, MarketSnapshot, SimpleTouch

_ZERO: Final = Decimal("0")
_ZERO_LATENCY: Final = timedelta(0)
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ns|us|ms|s|m)$")


class RemainderPolicy(StrEnum):
    """Venue-side treatment represented by a plugin after a partial fill."""

    CANCEL = "cancel"
    REST = "rest"


@dataclass(frozen=True, slots=True)
class LatencySizeConfig:
    """Independent configuration for :class:`LatencySize`.

    ``response_latency`` is metadata consumed by an execution adapter.  It
    does not use wall-clock time and does not alter the immutable decision
    snapshot returned by the plugin.
    """

    response_latency: timedelta = _ZERO_LATENCY
    remainder_policy: RemainderPolicy = RemainderPolicy.REST
    require_trade_through: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.response_latency, timedelta):
            raise TypeError("response_latency must be a timedelta")
        if self.response_latency < _ZERO_LATENCY:
            raise ValueError("response_latency must not be negative")
        object.__setattr__(
            self, "remainder_policy", RemainderPolicy(self.remainder_policy)
        )
        if not isinstance(self.require_trade_through, bool):
            raise TypeError("require_trade_through must be a bool")

    @property
    def latency(self) -> timedelta:
        """Compatibility name for adapters that call the value ``latency``."""
        return self.response_latency


@dataclass(frozen=True, slots=True)
class LatencyMetadata:
    """Stable execution metadata accompanying a latency-size plugin."""

    response_latency: timedelta
    displayed_size_constrained: bool
    remainder_policy: RemainderPolicy
    require_trade_through: bool

    @property
    def latency(self) -> timedelta:
        """Return the configured response delay."""
        return self.response_latency

    @property
    def response_latency_us(self) -> int:
        """Return the delay in exact integer microseconds."""
        return self.response_latency // timedelta(microseconds=1)


@dataclass(slots=True)
class LatencySize:
    """Fill single-leg orders with deterministic latency and size rules.

    Marketable orders use the visible quote side.  A non-marketable limit
    order is pessimistically resting by default and needs a qualifying trade
    print.  Setting ``require_trade_through`` to ``False`` enables the
    explicitly optimistic alternative of filling a limit inside the current
    spread at its limit price.

    The model is intentionally stateful only for the cancel remainder policy:
    each order identifier is independently marked when its first decision is
    partial.  The caller still supplies the authoritative remaining quantity
    on every invocation, as required by the SimpleTouch/SimulatedVenue
    boundary.
    """

    response_latency: timedelta = _ZERO_LATENCY
    remainder_policy: RemainderPolicy = RemainderPolicy.REST
    require_trade_through: bool = True
    _cancelled: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        normalized = LatencySizeConfig(
            response_latency=self.response_latency,
            remainder_policy=self.remainder_policy,
            require_trade_through=self.require_trade_through,
        )
        object.__setattr__(self, "response_latency", normalized.response_latency)
        object.__setattr__(self, "remainder_policy", normalized.remainder_policy)
        object.__setattr__(
            self, "require_trade_through", normalized.require_trade_through
        )

    @classmethod
    def from_config(cls, config: LatencySizeConfig) -> LatencySize:
        """Build a plugin from an independent, immutable configuration."""
        if not isinstance(config, LatencySizeConfig):
            raise TypeError("config must be a LatencySizeConfig")
        return cls(
            response_latency=config.response_latency,
            remainder_policy=config.remainder_policy,
            require_trade_through=config.require_trade_through,
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> LatencySize:
        """Load one model without consulting the global harness configuration."""
        return cls.from_config(load_latency_size_config(path))

    @property
    def latency(self) -> timedelta:
        """Return response-latency metadata for a venue adapter."""
        return self.response_latency

    @property
    def metadata(self) -> LatencyMetadata:
        """Return immutable assumptions used by this model."""
        return LatencyMetadata(
            response_latency=self.response_latency,
            displayed_size_constrained=True,
            remainder_policy=self.remainder_policy,
            require_trade_through=self.require_trade_through,
        )

    @property
    def cancelled_order_ids(self) -> frozenset[str]:
        """Return order identifiers whose remainder was canceled by policy."""
        return frozenset(self._cancelled)

    @property
    def canceled_order_ids(self) -> frozenset[str]:
        """US-spelling alias for :attr:`cancelled_order_ids`."""
        return self.cancelled_order_ids

    def is_cancelled(self, order_id: str) -> bool:
        """Return whether this model has canceled an order's remainder."""
        return order_id in self._cancelled

    is_canceled = is_cancelled

    def decide(
        self,
        order: SingleLegOrder,
        market: MarketSnapshot,
        remaining_quantity: Decimal | None = None,
        *,
        available_quantity: Decimal | None = None,
        stop_triggered: bool = False,
    ) -> FillDecision | None:
        """Return one size-bounded decision, or ``None`` when not executable."""
        self._validate(order, market)
        if order.order_id in self._cancelled:
            return None
        remaining = (
            order.quantity
            if remaining_quantity is None
            else exact_decimal(remaining_quantity, "remaining_quantity")
        )
        if remaining <= _ZERO:
            raise ValueError("remaining_quantity must be positive")
        available = self._available(order.side, market, available_quantity)
        if available is None:
            return None

        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if not stop_triggered and not SimpleTouch().is_triggered(order, market):
                return None

        execution = self._execution(order, market)
        if execution is None:
            return None
        price, liquidity = execution
        quantity = min(remaining, available)
        if quantity <= _ZERO:
            return None
        decision = FillDecision(
            order_id=order.order_id,
            quantity=quantity,
            price=price,
            decision_price=price,
            triggering_market=market,
            liquidity=liquidity,
        )
        if quantity < remaining and self.remainder_policy is RemainderPolicy.CANCEL:
            self._cancelled.add(order.order_id)
        return decision

    def __call__(
        self,
        order: SingleLegOrder,
        market: MarketSnapshot,
        remaining_quantity: Decimal | None = None,
        *,
        available_quantity: Decimal | None = None,
        stop_triggered: bool = False,
    ) -> FillDecision | None:
        """Use the model as a SimpleTouch-compatible callable plugin."""
        return self.decide(
            order,
            market,
            remaining_quantity,
            available_quantity=available_quantity,
            stop_triggered=stop_triggered,
        )

    evaluate = decide
    fill = decide

    def _execution(
        self, order: SingleLegOrder, market: MarketSnapshot
    ) -> tuple[Decimal, str] | None:
        if isinstance(market, TradeEvent):
            if order.order_type in (OrderType.MARKET, OrderType.STOP):
                return market.price, "taker"
            if self._qualifying_print(order, market.price):
                return market.price, "maker"
            return None

        if order.side is Side.BUY:
            price = market.ask_price
            if order.order_type in (OrderType.MARKET, OrderType.STOP):
                return price, "taker"
            if price <= self._required_limit(order):
                return price, "taker"
            limit = self._required_limit(order)
            if (
                not self.require_trade_through
                and market.bid_price < limit < market.ask_price
            ):
                return limit, "maker"
            return None

        price = market.bid_price
        if order.order_type in (OrderType.MARKET, OrderType.STOP):
            return price, "taker"
        if price >= self._required_limit(order):
            return price, "taker"
        limit = self._required_limit(order)
        if (
            not self.require_trade_through
            and market.bid_price < limit < market.ask_price
        ):
            return limit, "maker"
        return None

    @staticmethod
    def _required_limit(order: SingleLegOrder) -> Decimal:
        if order.limit_price is None:
            raise ValueError("limit order is missing limit_price")
        return order.limit_price

    @staticmethod
    def _qualifying_print(order: SingleLegOrder, price: Decimal) -> bool:
        limit = LatencySize._required_limit(order)
        return price <= limit if order.side is Side.BUY else price >= limit

    @staticmethod
    def _available(
        side: Side,
        market: MarketSnapshot,
        available_quantity: Decimal | None,
    ) -> Decimal | None:
        if available_quantity is not None:
            available = exact_decimal(available_quantity, "available_quantity")
            return None if available <= _ZERO else available
        if isinstance(market, TradeEvent):
            return market.quantity
        return market.ask_quantity if side is Side.BUY else market.bid_quantity

    @staticmethod
    def _validate(order: SingleLegOrder, market: MarketSnapshot) -> None:
        if not isinstance(order, SingleLegOrder):
            raise TypeError("order must be a SingleLegOrder")
        if not isinstance(market, (QuoteEvent, TradeEvent)):
            raise TypeError("market must be a QuoteEvent or TradeEvent")
        if order.symbol != market.symbol:
            raise ValueError("order and market symbols must match")


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{field_name} keys must be strings")
    return value


def _duration(value: object, field_name: str) -> timedelta:
    if isinstance(value, timedelta):
        result = value
    elif isinstance(value, Mapping):
        values = _mapping(value, field_name)
        if set(values) != {"microseconds"}:
            raise ValueError(f"{field_name} must contain only microseconds")
        result = _duration_microseconds(values["microseconds"], field_name)
    elif isinstance(value, str):
        match = _DURATION.fullmatch(value.strip())
        if match is None:
            result = _duration_microseconds(value, field_name)
        else:
            amount = exact_decimal(match.group(1), field_name)
            factors = {
                "ns": Decimal("0.001"),
                "us": Decimal("1"),
                "ms": Decimal("1000"),
                "s": Decimal("1000000"),
                "m": Decimal("60000000"),
            }
            result = _duration_microseconds(
                amount * factors[match.group(2)], field_name
            )
    else:
        result = _duration_microseconds(value, field_name)
    if result < _ZERO_LATENCY:
        raise ValueError(f"{field_name} must not be negative")
    return result


def _duration_microseconds(value: object, field_name: str) -> timedelta:
    amount = exact_decimal(value, field_name)
    if amount != amount.to_integral_value():
        raise ValueError(f"{field_name} must resolve to whole microseconds")
    try:
        return timedelta(microseconds=int(amount))
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{field_name} is outside timedelta range") from error


def _selected(
    values: Mapping[str, object], names: tuple[str, ...], default: object
) -> object:
    present = [name for name in names if name in values]
    if len(present) > 1:
        raise ValueError(f"configuration contains duplicate aliases: {present}")
    return default if not present else values[present[0]]


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a bool")
    return value


def load_latency_size_config(path: str | Path) -> LatencySizeConfig:
    """Load a standalone latency-size YAML document.

    The loader accepts exact microsecond values or a small duration mapping;
    it never reads or mutates the execution-shell configuration.
    """
    values = load_yaml_mapping(path)
    allowed = {
        "schema_version",
        "model",
        "response_latency",
        "response_latency_us",
        "response_latency_microseconds",
        "latency_microseconds",
        "remainder_policy",
        "remainder",
        "require_trade_through",
        "trade_through",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(
            f"latency-size fields are invalid: unexpected {sorted(unknown)}"
        )
    if "schema_version" in values and (
        values["schema_version"] != 1 or isinstance(values["schema_version"], bool)
    ):
        raise ValueError("latency-size schema_version must be 1")
    if "model" in values and values["model"] != "latency_size":
        raise ValueError("latency-size model must be 'latency_size'")

    latency_value = _selected(
        values,
        (
            "response_latency",
            "response_latency_us",
            "response_latency_microseconds",
            "latency_microseconds",
        ),
        _ZERO_LATENCY,
    )
    remainder_value = _selected(values, ("remainder_policy", "remainder"), "rest")
    if isinstance(remainder_value, Mapping):
        remainder_values = _mapping(remainder_value, "remainder")
        if set(remainder_values) != {"policy"}:
            raise ValueError("remainder must contain only policy")
        remainder_value = remainder_values["policy"]
    if "trade_through" in values and "require_trade_through" in values:
        raise ValueError("configure only one of trade_through or require_trade_through")
    trade_value = _selected(values, ("require_trade_through", "trade_through"), True)
    if isinstance(trade_value, str):
        trade_values = {"required": True, "pessimistic": True, "optional": False}
        try:
            trade_value = trade_values[trade_value]
        except KeyError as error:
            raise ValueError(
                "trade_through must be required, pessimistic, or optional"
            ) from error
    return LatencySizeConfig(
        response_latency=_duration(latency_value, "response_latency"),
        remainder_policy=RemainderPolicy(remainder_value),
        require_trade_through=_boolean(trade_value, "require_trade_through"),
    )


def load_latency_size(path: str | Path) -> LatencySize:
    """Load a configured :class:`LatencySize` plugin."""
    return LatencySize.from_config(load_latency_size_config(path))


LatencySizeFill = LatencySize
LatencySizeFillModel = LatencySize

__all__ = [
    "LatencyMetadata",
    "LatencySize",
    "LatencySizeConfig",
    "LatencySizeFill",
    "LatencySizeFillModel",
    "RemainderPolicy",
    "load_latency_size",
    "load_latency_size_config",
]
