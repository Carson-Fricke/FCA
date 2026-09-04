"""Pessimistic midpoint fill decisions for quoted option markets.

The documented adverse midpoint rule
------------------------------------
A limit order fills at the exact quote midpoint ``M = (bid + ask) / 2`` only
when the adverse touch is marketable through the limit: a buy requires
``ask <= limit`` and a sell requires ``bid >= limit``.  The executed price is
the midpoint, so a resting buy never pays more than its limit, never fills
while the far touch has not crossed its limit, and never fills an off-tick
midpoint.  Default fills are therefore never better than this documented
adverse midpoint rule.  Setting ``require_adverse_touch=False`` selects the
explicitly optimistic mid-cross trigger, which fills when the midpoint alone
crosses the limit; it is never the default.

Market and stop orders are pessimistic takers: they fill at the far touch of
a quote, or at a trade print, capped by displayed size.  A trade print alone
never qualifies a resting limit, because a midpoint requires a two-sided
quote.  Limit fills are maker liquidity; taker fills are market liquidity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Final

from harness.configuration.loader import load_yaml_mapping
from harness.core.types.common import DecimalInput, exact_decimal
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.core.types.orders import OrderType, Side, SingleLegOrder
from harness.fills.simple_touch import FillDecision, MarketSnapshot, SimpleTouch

_ZERO: Final = Decimal("0")
_TWO: Final = Decimal("2")
_DEFAULT_TICK: Final = Decimal("0.01")
_ZERO_LATENCY: Final = timedelta(0)
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ns|us|ms|s|m)$")


def _exponent(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError("value must be a finite decimal")
    return exponent


def _exact_midpoint(bid: Decimal, ask: Decimal) -> Decimal:
    """Return ``(bid + ask) / 2`` without binary floating point or rounding."""
    low = min(_exponent(bid), _exponent(ask))
    high = max(bid.adjusted(), ask.adjusted())
    with localcontext() as context:
        context.prec = max(high - low + 4, 28)
        return (bid + ask) / _TWO


def _coefficient(value: Decimal, exponent: int) -> int:
    sign, digits, _ = value.as_tuple()
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    coefficient *= 10 ** (_exponent(value) - exponent)
    return -coefficient if sign else coefficient


def _is_multiple(value: Decimal, tick: Decimal) -> bool:
    """Return whether ``value`` is an exact integer multiple of ``tick``."""
    exponent = min(_exponent(value), _exponent(tick))
    return _coefficient(value, exponent) % _coefficient(tick, exponent) == 0


@dataclass(frozen=True, slots=True)
class MidTouchConfig:
    """Independent configuration for :class:`MidTouch`.

    ``tick_size`` is the smallest valid midpoint increment.  ``maximum_spread``
    optionally rejects quotes wider than the gate.  ``require_adverse_touch``
    selects the documented pessimistic rule by default; the optimistic
    mid-cross trigger is an explicit, non-default choice.
    """

    response_latency: timedelta = _ZERO_LATENCY
    tick_size: DecimalInput = _DEFAULT_TICK
    maximum_spread: DecimalInput | None = None
    require_adverse_touch: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.response_latency, timedelta):
            raise TypeError("response_latency must be a timedelta")
        if self.response_latency < _ZERO_LATENCY:
            raise ValueError("response_latency must not be negative")
        tick = exact_decimal(self.tick_size, "tick_size")
        if tick <= _ZERO:
            raise ValueError("tick_size must be positive")
        object.__setattr__(self, "tick_size", tick)
        if self.maximum_spread is not None:
            spread = exact_decimal(self.maximum_spread, "maximum_spread")
            if spread < _ZERO:
                raise ValueError("maximum_spread must not be negative")
            object.__setattr__(self, "maximum_spread", spread)
        if not isinstance(self.require_adverse_touch, bool):
            raise TypeError("require_adverse_touch must be a bool")

    @property
    def latency(self) -> timedelta:
        """Compatibility name for adapters that call the value ``latency``."""
        return self.response_latency


@dataclass(frozen=True, slots=True)
class MidTouchMetadata:
    """Stable execution assumptions accompanying a midpoint-touch plugin."""

    response_latency: timedelta
    displayed_size_constrained: bool
    tick_size: Decimal
    maximum_spread: Decimal | None
    require_adverse_touch: bool

    @property
    def latency(self) -> timedelta:
        """Return the configured response delay."""
        return self.response_latency

    @property
    def response_latency_us(self) -> int:
        """Return the delay in exact integer microseconds."""
        return self.response_latency // timedelta(microseconds=1)


@dataclass(slots=True)
class MidTouch:
    """Fill single-leg orders under the documented adverse midpoint rule.

    Limit and stop-limit orders fill at the exact quote midpoint only when
    the adverse touch is marketable through the limit, the midpoint is an
    exact multiple of ``tick_size``, and the spread does not exceed the
    optional ``maximum_spread``.  Displayed touch size bounds every fill.
    The optimistic mid-cross trigger is available by setting
    ``require_adverse_touch=False``.
    """

    response_latency: timedelta = _ZERO_LATENCY
    tick_size: Decimal = _DEFAULT_TICK
    maximum_spread: Decimal | None = None
    require_adverse_touch: bool = True

    def __post_init__(self) -> None:
        normalized = MidTouchConfig(
            response_latency=self.response_latency,
            tick_size=self.tick_size,
            maximum_spread=self.maximum_spread,
            require_adverse_touch=self.require_adverse_touch,
        )
        object.__setattr__(self, "response_latency", normalized.response_latency)
        object.__setattr__(
            self, "tick_size", exact_decimal(normalized.tick_size, "tick_size")
        )
        maximum = normalized.maximum_spread
        object.__setattr__(
            self,
            "maximum_spread",
            None if maximum is None else exact_decimal(maximum, "maximum_spread"),
        )
        object.__setattr__(
            self, "require_adverse_touch", normalized.require_adverse_touch
        )

    @classmethod
    def from_config(cls, config: MidTouchConfig) -> MidTouch:
        """Build a plugin from an independent, immutable configuration."""
        if not isinstance(config, MidTouchConfig):
            raise TypeError("config must be a MidTouchConfig")
        return cls(
            response_latency=config.response_latency,
            tick_size=exact_decimal(config.tick_size, "tick_size"),
            maximum_spread=(
                None
                if config.maximum_spread is None
                else exact_decimal(config.maximum_spread, "maximum_spread")
            ),
            require_adverse_touch=config.require_adverse_touch,
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> MidTouch:
        """Load one model without consulting the global harness configuration."""
        return cls.from_config(load_mid_touch_config(path))

    @property
    def latency(self) -> timedelta:
        """Return response-latency metadata for a venue adapter."""
        return self.response_latency

    @property
    def metadata(self) -> MidTouchMetadata:
        """Return immutable assumptions used by this model."""
        return MidTouchMetadata(
            response_latency=self.response_latency,
            displayed_size_constrained=True,
            tick_size=self.tick_size,
            maximum_spread=self.maximum_spread,
            require_adverse_touch=self.require_adverse_touch,
        )

    def is_triggered(self, order: SingleLegOrder, market: MarketSnapshot) -> bool:
        """Return whether a stop order is armed by this market snapshot."""
        return SimpleTouch().is_triggered(order, market)

    triggered = is_triggered

    def decide(
        self,
        order: SingleLegOrder,
        market: MarketSnapshot,
        remaining_quantity: Decimal | None = None,
        *,
        available_quantity: Decimal | None = None,
        stop_triggered: bool = False,
    ) -> FillDecision | None:
        """Return one adverse-midpoint decision, or ``None``."""
        self._validate(order, market)
        remaining = (
            order.quantity
            if remaining_quantity is None
            else exact_decimal(remaining_quantity, "remaining_quantity")
        )
        if remaining <= _ZERO:
            raise ValueError("remaining_quantity must be positive")
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if not stop_triggered and not self.is_triggered(order, market):
                return None
        if isinstance(market, TradeEvent):
            return self._decide_trade(order, market, remaining, available_quantity)
        return self._decide_quote(order, market, remaining, available_quantity)

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

    def mid_execution(self, quote: QuoteEvent) -> Decimal | None:
        """Return the fillable exact midpoint for a quote, or ``None``.

        The midpoint is rejected when the spread exceeds the configured
        maximum or the midpoint is not an exact multiple of ``tick_size``.
        """
        if not isinstance(quote, QuoteEvent):
            raise TypeError("quote must be a QuoteEvent")
        spread = quote.ask_price - quote.bid_price
        if self.maximum_spread is not None and spread > self.maximum_spread:
            return None
        midpoint = _exact_midpoint(quote.bid_price, quote.ask_price)
        if not _is_multiple(midpoint, self.tick_size):
            return None
        return midpoint

    def _decide_trade(
        self,
        order: SingleLegOrder,
        market: TradeEvent,
        remaining: Decimal,
        available_quantity: Decimal | None,
    ) -> FillDecision | None:
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            return None
        available = self._available(available_quantity, market.quantity)
        if available is None:
            return None
        quantity = min(remaining, available)
        if quantity <= _ZERO:
            return None
        return FillDecision(
            order_id=order.order_id,
            quantity=quantity,
            price=market.price,
            decision_price=market.price,
            triggering_market=market,
        )

    def _decide_quote(
        self,
        order: SingleLegOrder,
        market: QuoteEvent,
        remaining: Decimal,
        available_quantity: Decimal | None,
    ) -> FillDecision | None:
        if order.order_type in (OrderType.MARKET, OrderType.STOP):
            price = market.ask_price if order.side is Side.BUY else market.bid_price
            displayed = (
                market.ask_quantity if order.side is Side.BUY else market.bid_quantity
            )
            available = self._available(available_quantity, displayed)
            if available is None:
                return None
            quantity = min(remaining, available)
            if quantity <= _ZERO:
                return None
            return FillDecision(
                order_id=order.order_id,
                quantity=quantity,
                price=price,
                decision_price=price,
                triggering_market=market,
            )
        limit = order.limit_price
        if limit is None:
            raise ValueError("limit order is missing limit_price")
        midpoint = self.mid_execution(market)
        if midpoint is None:
            return None
        if order.side is Side.BUY:
            if self.require_adverse_touch:
                if market.ask_price > limit:
                    return None
            elif midpoint > limit:
                return None
            displayed = market.ask_quantity
        else:
            if self.require_adverse_touch:
                if market.bid_price < limit:
                    return None
            elif midpoint < limit:
                return None
            displayed = market.bid_quantity
        available = self._available(available_quantity, displayed)
        if available is None:
            return None
        quantity = min(remaining, available)
        if quantity <= _ZERO:
            return None
        return FillDecision(
            order_id=order.order_id,
            quantity=quantity,
            price=midpoint,
            decision_price=midpoint,
            triggering_market=market,
            liquidity="maker",
        )

    @staticmethod
    def _available(
        available_quantity: Decimal | None, displayed: Decimal
    ) -> Decimal | None:
        if available_quantity is not None:
            available = exact_decimal(available_quantity, "available_quantity")
            return None if available <= _ZERO else available
        return displayed

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


def load_mid_touch_config(path: str | Path) -> MidTouchConfig:
    """Load a standalone adverse-midpoint YAML document."""
    values = load_yaml_mapping(path)
    allowed = {
        "schema_version",
        "model",
        "response_latency",
        "response_latency_us",
        "response_latency_microseconds",
        "latency_microseconds",
        "tick_size",
        "maximum_spread",
        "require_adverse_touch",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"mid-touch fields are invalid: unexpected {sorted(unknown)}")
    if "schema_version" in values and (
        values["schema_version"] != 1 or isinstance(values["schema_version"], bool)
    ):
        raise ValueError("mid-touch schema_version must be 1")
    if "model" in values and values["model"] != "mid_touch":
        raise ValueError("mid-touch model must be 'mid_touch'")
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
    return MidTouchConfig(
        response_latency=_duration(latency_value, "response_latency"),
        tick_size=exact_decimal(values.get("tick_size", _DEFAULT_TICK), "tick_size"),
        maximum_spread=(
            None
            if values.get("maximum_spread") is None
            else exact_decimal(values["maximum_spread"], "maximum_spread")
        ),
        require_adverse_touch=_boolean(
            values.get("require_adverse_touch", True), "require_adverse_touch"
        ),
    )


def load_mid_touch(path: str | Path) -> MidTouch:
    """Load a configured :class:`MidTouch` plugin."""
    return MidTouch.from_config(load_mid_touch_config(path))


MidTouchFill = MidTouch
MidTouchFillModel = MidTouch

__all__ = [
    "MidTouch",
    "MidTouchConfig",
    "MidTouchFill",
    "MidTouchFillModel",
    "MidTouchMetadata",
    "load_mid_touch",
    "load_mid_touch_config",
]
