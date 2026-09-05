"""Focused tests for the strategy corporate-action hook and delivery."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types import QuoteEvent
from harness.data.listing_events import (
    CorporateActionEvent,
    DelistingEvent,
    RenameEvent,
)
from harness.portfolio import PortfolioHub
from harness.strategy import (
    CALLBACK_ORDER,
    ReferenceStrategy,
    Strategy,
    StrategyContext,
    bind_strategy,
)
from harness.venues import ExecutionVenue
from lake.catalog.models import DelistingType

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
AAPL = Symbol("AAPL")

DELISTING = DelistingEvent(
    entity_id="TWTR",
    known_date=date(2022, 10, 28),
    symbol="TWTR",
    delisting_type=DelistingType.ACQUISITION,
    final_price=Decimal("53.70"),
)
RENAME = RenameEvent(
    entity_id="META",
    known_date=date(2022, 6, 9),
    old_symbol="FB",
    new_symbol="META",
)


class ActionAwareStrategy(Strategy):
    def __init__(self) -> None:
        self.context: StrategyContext | None = None
        self.actions: list[tuple[str, CorporateActionEvent]] = []

    def on_corporate_action(
        self, event: CorporateActionEvent, context: StrategyContext
    ) -> None:
        self.actions.append((type(event).__name__, event))
        self.context = context


class _NullVenue:
    def submit(self, order: object) -> None:
        pass

    def cancel(self, order_id: str) -> None:
        pass

    def replace(self, order_id: str, replacement: object) -> None:
        pass

    def request_position_snapshot(self, account_id: str) -> None:
        pass


def _context(hub: PortfolioHub | None = None) -> tuple[EventBus, StrategyContext]:
    clock = SimClock(STAMP)
    bus = EventBus(clock)
    book_hub = hub or PortfolioHub("account")
    if not book_hub.book_ids:
        book_hub.create_book("strategy", "1000")
    context = StrategyContext(
        clock,
        portfolio=book_hub,
        bus=bus,
        universe=(AAPL,),
        book_id="strategy",
        seed=1729,
    )
    return bus, context


def _deliver(
    strategy: Strategy, context: StrategyContext, event: CorporateActionEvent
) -> None:
    """Mirror the reported binding wiring for one engine-delivered event."""
    if context._deliver_corporate_action(event):  # noqa: SLF001
        strategy.on_corporate_action(event, context)


def test_callback_order_places_the_hook_before_on_stop() -> None:
    assert CALLBACK_ORDER == (
        "on_start",
        "on_quote",
        "on_trade",
        "on_order",
        "on_execution_report",
        "on_fill",
        "on_position",
        "on_book",
        "on_account",
        "on_corporate_action",
        "on_stop",
    )


def test_default_hook_is_a_noop() -> None:
    _, context = _context()

    assert Strategy().on_corporate_action(DELISTING, context) is None


def test_hook_receives_the_event_and_the_context() -> None:
    _, context = _context()
    binding = bind_strategy(ActionAwareStrategy, None, context)
    binding.start()
    recorder = binding.strategy
    assert isinstance(recorder, ActionAwareStrategy)

    _deliver(recorder, context, DELISTING)

    assert recorder.actions == [("DelistingEvent", DELISTING)]
    assert recorder.context is context


def test_delivery_is_idempotent_per_event() -> None:
    _, context = _context()
    binding = bind_strategy(ActionAwareStrategy, None, context)
    binding.start()
    recorder = binding.strategy
    assert isinstance(recorder, ActionAwareStrategy)

    _deliver(recorder, context, DELISTING)
    _deliver(recorder, context, DELISTING)

    assert recorder.actions == [("DelistingEvent", DELISTING)]
    assert context.corporate_actions == (DELISTING,)


def test_context_records_events_in_delivery_order() -> None:
    _, context = _context()
    binding = bind_strategy(ActionAwareStrategy, None, context)
    binding.start()
    recorder = binding.strategy
    assert isinstance(recorder, ActionAwareStrategy)

    _deliver(recorder, context, RENAME)
    _deliver(recorder, context, DELISTING)

    assert recorder.actions == [
        ("RenameEvent", RENAME),
        ("DelistingEvent", DELISTING),
    ]
    assert context.corporate_actions == (RENAME, DELISTING)


def test_delivery_rejects_non_corporate_action_events() -> None:
    _, context = _context()
    quote = QuoteEvent(
        "q", STAMP, 1, AAPL, Decimal("1"), Decimal("1"), Decimal("2"), Decimal("1")
    )

    with pytest.raises(TypeError, match="CorporateActionEvent"):
        context._deliver_corporate_action(  # noqa: SLF001
            cast(CorporateActionEvent, quote)
        )


def test_reference_strategy_inherits_the_default_noop() -> None:
    assert ReferenceStrategy.on_corporate_action is Strategy.on_corporate_action


def test_venue_and_venueless_contexts_both_expose_the_hook_surface() -> None:
    clock = SimClock(STAMP)
    bus = EventBus(clock)
    hub = PortfolioHub("account")
    hub.create_book("strategy", "1000")
    with_venue = StrategyContext(
        clock,
        portfolio=hub,
        bus=bus,
        venue=cast(ExecutionVenue, _NullVenue()),
        universe=(AAPL,),
        book_id="strategy",
    )
    without_venue = StrategyContext(
        clock,
        portfolio=hub,
        bus=bus,
        universe=(AAPL,),
        book_id="strategy",
    )

    assert with_venue.corporate_actions == ()
    assert without_venue.corporate_actions == ()
