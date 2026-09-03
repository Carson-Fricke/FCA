"""Strict deterministic trade-print fill decisions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from harness.configuration.loader import load_yaml_mapping
from harness.core.types.common import exact_decimal
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.core.types.orders import OrderType, Side, SingleLegOrder
from harness.fills.latency_size import RemainderPolicy
from harness.fills.simple_touch import FillDecision, MarketSnapshot, SimpleTouch

_ZERO: Final = Decimal("0")
_ZERO_LATENCY: Final = timedelta(0)
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ns|us|ms|s|m)$")


@dataclass(frozen=True, slots=True)
class TradeThroughConfig:
    """Independent configuration for the strict print-only model."""

    response_latency: timedelta = _ZERO_LATENCY
    minimum_trade_through: Decimal = _ZERO
    strict: bool = True
    remainder_policy: RemainderPolicy = RemainderPolicy.REST

    def __post_init__(self) -> None:
        if not isinstance(self.response_latency, timedelta):
            raise TypeError("response_latency must be a timedelta")
        if self.response_latency < _ZERO_LATENCY:
            raise ValueError("response_latency must not be negative")
        minimum = exact_decimal(self.minimum_trade_through, "minimum_trade_through")
        if minimum < _ZERO:
            raise ValueError("minimum_trade_through must not be negative")
        object.__setattr__(self, "minimum_trade_through", minimum)
        if not isinstance(self.strict, bool):
            raise TypeError("strict must be a bool")
        object.__setattr__(
            self, "remainder_policy", RemainderPolicy(self.remainder_policy)
        )


@dataclass(frozen=True, slots=True)
class TradeThroughMetadata:
    """Stable assumptions for a print-only trade-through plugin."""

    response_latency: timedelta
    displayed_size_constrained: bool
    minimum_trade_through: Decimal
    strict: bool
    remainder_policy: RemainderPolicy

    @property
    def latency(self) -> timedelta:
        """Return the configured response delay."""
        return self.response_latency

    @property
    def response_latency_us(self) -> int:
        """Return the delay in exact integer microseconds."""
        return self.response_latency // timedelta(microseconds=1)


@dataclass(slots=True)
class TradeThrough:
    """Require qualifying trade prints for every executable order.

    Quote events never fill this model, including marketable limits.  A buy
    limit requires a print strictly below its limit by default; a sell limit
    requires a print strictly above it.  ``minimum_trade_through`` adds an
    exact price distance.  Setting ``strict=False`` makes a print at the limit
    qualifying while retaining the print-only rule.
    """

    response_latency: timedelta = _ZERO_LATENCY
    minimum_trade_through: Decimal = _ZERO
    strict: bool = True
    remainder_policy: RemainderPolicy = RemainderPolicy.REST
    _cancelled: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        normalized = TradeThroughConfig(
            response_latency=self.response_latency,
            minimum_trade_through=self.minimum_trade_through,
            strict=self.strict,
            remainder_policy=self.remainder_policy,
        )
        object.__setattr__(self, "response_latency", normalized.response_latency)
        object.__setattr__(
            self, "minimum_trade_through", normalized.minimum_trade_through
        )
        object.__setattr__(self, "strict", normalized.strict)
        object.__setattr__(self, "remainder_policy", normalized.remainder_policy)

    @classmethod
    def from_config(cls, config: TradeThroughConfig) -> TradeThrough:
        """Build a plugin from an independent, immutable configuration."""
        if not isinstance(config, TradeThroughConfig):
            raise TypeError("config must be a TradeThroughConfig")
        return cls(
            response_latency=config.response_latency,
            minimum_trade_through=config.minimum_trade_through,
            strict=config.strict,
            remainder_policy=config.remainder_policy,
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> TradeThrough:
        """Load one model without consulting the global harness configuration."""
        return cls.from_config(load_trade_through_config(path))

    @property
    def latency(self) -> timedelta:
        """Return response-latency metadata for a venue adapter."""
        return self.response_latency

    @property
    def metadata(self) -> TradeThroughMetadata:
        """Return immutable assumptions used by this model."""
        return TradeThroughMetadata(
            response_latency=self.response_latency,
            displayed_size_constrained=True,
            minimum_trade_through=self.minimum_trade_through,
            strict=self.strict,
            remainder_policy=self.remainder_policy,
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
        """Return one trade-print-bounded decision, or ``None``."""
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
        if not isinstance(market, TradeEvent):
            return None
        available = self._available(market, available_quantity)
        if available is None:
            return None

        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if not stop_triggered and not SimpleTouch().is_triggered(order, market):
                return None
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            if not self._qualifies(order, market.price):
                return None

        quantity = min(remaining, available)
        if quantity <= _ZERO:
            return None
        liquidity = (
            "maker"
            if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
            else "taker"
        )
        decision = FillDecision(
            order_id=order.order_id,
            quantity=quantity,
            price=market.price,
            decision_price=market.price,
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

    def _qualifies(self, order: SingleLegOrder, price: Decimal) -> bool:
        if order.limit_price is None:
            raise ValueError("limit order is missing limit_price")
        if order.side is Side.BUY:
            threshold = order.limit_price - self.minimum_trade_through
            return price < threshold if self.strict else price <= threshold
        threshold = order.limit_price + self.minimum_trade_through
        return price > threshold if self.strict else price >= threshold

    @staticmethod
    def _available(
        market: TradeEvent, available_quantity: Decimal | None
    ) -> Decimal | None:
        if available_quantity is not None:
            available = exact_decimal(available_quantity, "available_quantity")
            return None if available <= _ZERO else available
        return market.quantity

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
    if isinstance(value, Mapping):
        values = _mapping(value, field_name)
        if set(values) != {"microseconds"}:
            raise ValueError(f"{field_name} must contain only microseconds")
        value = values["microseconds"]
    elif isinstance(value, str):
        match = _DURATION.fullmatch(value.strip())
        if match is not None:
            amount = exact_decimal(match.group(1), field_name)
            factors = {
                "ns": Decimal("0.001"),
                "us": Decimal("1"),
                "ms": Decimal("1000"),
                "s": Decimal("1000000"),
                "m": Decimal("60000000"),
            }
            value = amount * factors[match.group(2)]
    amount = exact_decimal(value, field_name)
    if amount < _ZERO or amount != amount.to_integral_value():
        raise ValueError(f"{field_name} must be non-negative whole microseconds")
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


def load_trade_through_config(path: str | Path) -> TradeThroughConfig:
    """Load a standalone print-only trade-through YAML document."""
    values = load_yaml_mapping(path)
    allowed = {
        "schema_version",
        "model",
        "response_latency",
        "response_latency_us",
        "response_latency_microseconds",
        "latency_microseconds",
        "minimum_trade_through",
        "minimum_price_improvement",
        "strict",
        "remainder_policy",
        "remainder",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(
            f"trade-through fields are invalid: unexpected {sorted(unknown)}"
        )
    if "schema_version" in values and (
        values["schema_version"] != 1 or isinstance(values["schema_version"], bool)
    ):
        raise ValueError("trade-through schema_version must be 1")
    if "model" in values and values["model"] != "trade_through":
        raise ValueError("trade-through model must be 'trade_through'")

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
    minimum_value = _selected(
        values,
        ("minimum_trade_through", "minimum_price_improvement"),
        _ZERO,
    )
    remainder_value = _selected(values, ("remainder_policy", "remainder"), "rest")
    if isinstance(remainder_value, Mapping):
        remainder_values = _mapping(remainder_value, "remainder")
        if set(remainder_values) != {"policy"}:
            raise ValueError("remainder must contain only policy")
        remainder_value = remainder_values["policy"]
    return TradeThroughConfig(
        response_latency=_duration(latency_value, "response_latency"),
        minimum_trade_through=exact_decimal(minimum_value, "minimum_trade_through"),
        strict=_boolean(values.get("strict", True), "strict"),
        remainder_policy=RemainderPolicy(remainder_value),
    )


def load_trade_through(path: str | Path) -> TradeThrough:
    """Load a configured :class:`TradeThrough` plugin."""
    return TradeThrough.from_config(load_trade_through_config(path))


TradeThroughFill = TradeThrough
TradeThroughFillModel = TradeThrough

__all__ = [
    "TradeThrough",
    "TradeThroughConfig",
    "TradeThroughFill",
    "TradeThroughFillModel",
    "TradeThroughMetadata",
    "load_trade_through",
    "load_trade_through_config",
]
