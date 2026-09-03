"""Deterministic top-of-book touch decisions for single-leg orders."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from harness.core.symbols import Symbol
from harness.core.types.common import exact_decimal, text
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.core.types.orders import OrderType, Side, SingleLegOrder

type MarketSnapshot = QuoteEvent | TradeEvent


@dataclass(frozen=True, slots=True)
class FillDecision:
    """One exact fill decision and the immutable market event that caused it."""

    order_id: str
    quantity: Decimal
    price: Decimal
    decision_price: Decimal
    triggering_market: MarketSnapshot
    liquidity: str = "taker"

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", text(self.order_id, "order_id"))
        object.__setattr__(self, "quantity", exact_decimal(self.quantity, "quantity"))
        object.__setattr__(self, "price", exact_decimal(self.price, "price"))
        object.__setattr__(
            self,
            "decision_price",
            exact_decimal(self.decision_price, "decision_price"),
        )
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.price <= 0 or self.decision_price <= 0:
            raise ValueError("price and decision_price must be positive")
        if not isinstance(self.triggering_market, (QuoteEvent, TradeEvent)):
            raise TypeError("triggering_market must be a market event")
        if self.liquidity not in {"maker", "taker"}:
            raise ValueError("liquidity must be maker or taker")

    @property
    def fill_price(self) -> Decimal:
        """Return the execution price under the common fill vocabulary."""
        return self.price

    @property
    def market(self) -> MarketSnapshot:
        """Return the exact market snapshot used for this decision."""
        return self.triggering_market

    @property
    def market_snapshot(self) -> MarketSnapshot:
        """Return the exact market snapshot used for this decision."""
        return self.triggering_market

    @property
    def triggering_market_snapshot(self) -> MarketSnapshot:
        """Return the exact market snapshot used for this decision."""
        return self.triggering_market

    @property
    def market_event(self) -> MarketSnapshot:
        """Return the market event that produced this decision."""
        return self.triggering_market

    @property
    def execution_price(self) -> Decimal:
        """Return the exact price used for the fill."""
        return self.price

    @property
    def symbol(self) -> Symbol:
        """Return the symbol from the triggering market snapshot."""
        return self.triggering_market.symbol


class SimpleTouch:
    """Fill a single-leg order at the first visible quote or trade touch.

    Quotes use the ask for buys and bid for sells.  A quote's displayed side
    quantity bounds a fill, which gives the simulated venue deterministic
    partial-fill behavior without a random participation model.
    """

    __slots__ = ()

    def decide(
        self,
        order: SingleLegOrder,
        market: MarketSnapshot,
        remaining_quantity: Decimal | None = None,
        *,
        available_quantity: Decimal | None = None,
        stop_triggered: bool = False,
    ) -> FillDecision | None:
        """Return a fill decision, or ``None`` when the order does not touch."""
        self._validate(order, market)
        remaining = (
            order.quantity
            if remaining_quantity is None
            else exact_decimal(remaining_quantity, "remaining_quantity")
        )
        if remaining <= 0:
            raise ValueError("remaining_quantity must be positive")
        if available_quantity is not None:
            available = exact_decimal(available_quantity, "available_quantity")
            if available <= 0:
                return None
        else:
            available = self._market_quantity(order.side, market)

        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if not stop_triggered and not self.is_triggered(order, market):
                return None

        price, touched = self._touch(order, market)
        if not touched:
            return None

        quantity = min(remaining, available)
        if quantity <= 0:
            return None
        return FillDecision(
            order_id=order.order_id,
            quantity=quantity,
            price=price,
            decision_price=price,
            triggering_market=market,
        )

    def __call__(
        self,
        order: SingleLegOrder,
        market: MarketSnapshot,
        remaining_quantity: Decimal | None = None,
        *,
        available_quantity: Decimal | None = None,
        stop_triggered: bool = False,
    ) -> FillDecision | None:
        """Use the fill model as a small callable plugin."""
        return self.decide(
            order,
            market,
            remaining_quantity,
            available_quantity=available_quantity,
            stop_triggered=stop_triggered,
        )

    def is_triggered(self, order: SingleLegOrder, market: MarketSnapshot) -> bool:
        """Return whether a stop order is armed by this market snapshot."""
        self._validate(order, market)
        if order.order_type not in (OrderType.STOP, OrderType.STOP_LIMIT):
            return True
        if order.stop_price is None:
            raise ValueError("stop order is missing stop_price")
        if isinstance(market, TradeEvent):
            return (
                market.price >= order.stop_price
                if order.side is Side.BUY
                else market.price <= order.stop_price
            )
        return (
            market.ask_price >= order.stop_price
            if order.side is Side.BUY
            else market.bid_price <= order.stop_price
        )

    # These aliases keep the plugin convenient for venue adapters without
    # introducing a second fill-decision protocol.
    triggered = is_triggered
    evaluate = decide
    fill = decide

    @staticmethod
    def _validate(order: SingleLegOrder, market: MarketSnapshot) -> None:
        if not isinstance(order, SingleLegOrder):
            raise TypeError("order must be a SingleLegOrder")
        if not isinstance(market, (QuoteEvent, TradeEvent)):
            raise TypeError("market must be a QuoteEvent or TradeEvent")
        if order.symbol != market.symbol:
            raise ValueError("order and market symbols must match")

    @staticmethod
    def _market_quantity(side: Side, market: MarketSnapshot) -> Decimal:
        if isinstance(market, TradeEvent):
            return market.quantity
        return market.ask_quantity if side is Side.BUY else market.bid_quantity

    @staticmethod
    def _touch(order: SingleLegOrder, market: MarketSnapshot) -> tuple[Decimal, bool]:
        if isinstance(market, TradeEvent):
            price = market.price
            if order.order_type in (OrderType.MARKET, OrderType.STOP):
                return price, True
            # A trade alone is not a SimpleTouch for a resting limit.  Print
            # qualification belongs to the later trade-through fill model.
            return price, False

        if order.side is Side.BUY:
            price = market.ask_price
            if order.order_type in (OrderType.MARKET, OrderType.STOP):
                return price, True
            if order.limit_price is None:
                raise ValueError("limit order is missing limit_price")
            return price, price <= order.limit_price

        price = market.bid_price
        if order.order_type in (OrderType.MARKET, OrderType.STOP):
            return price, True
        if order.limit_price is None:
            raise ValueError("limit order is missing limit_price")
        return price, price >= order.limit_price


SimpleTouchFill = SimpleTouch
TouchDecision = FillDecision

__all__ = [
    "FillDecision",
    "MarketSnapshot",
    "SimpleTouch",
    "SimpleTouchFill",
    "TouchDecision",
]
