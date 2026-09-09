"""Two-boot restart-cycle tests for the paper runtime snapshot restore.

The tests reproduce the 2026-09-08 paper incident: a restored shell re-entered
the strategy position and the simulated venue regenerated a fill identifier
the restored book had already routed, so the shell aborted fail-closed with
``DuplicateFillError``.  Each test boots one shell over a scripted feed,
captures and saves a snapshot, restores a second shell from it exactly as the
supervisor does, and drives new quotes through the restored runtime.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from strategies.harness_examples.buy_and_hold import BuyAndHoldStrategy

from harness.configuration import (
    StrategyInstanceConfig,
    TraderConfig,
    load_example,
)
from harness.core.symbols import Symbol
from harness.core.types import (
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.runtime.paper import PaperOptions, PaperShell, VenueWatermarks
from harness.runtime.state_store import RuntimeStateStore, SnapshotTrigger
from harness.strategy import Strategy, StrategyContext

RUN_ID = "restart-cycle-1"
CASH = Decimal("100000")
INSTANCE_ID = "spy-momentum-paper"
OPEN = datetime(2024, 1, 2, 13, 30, tzinfo=UTC)
STRATEGY_MODULE = "restart_cycle_test"


class RestartCycleStrategy(Strategy):
    """Submit one deterministic market order every ``every`` quotes.

    The quote counter is the strategy's exported ``StateStore`` state, so a
    snapshot restore continues the cadence and the order identifiers exactly
    where the captured runtime stopped.
    """

    def __init__(self, **parameters: object) -> None:
        self.quantity = Decimal(str(parameters.get("quantity", "10")))
        self.every = int(str(parameters.get("every", "2")))
        self.quotes = 0
        self.context: StrategyContext | None = None

    def on_start(self, context: StrategyContext) -> None:
        self.context = context

    def on_quote(self, event: QuoteEvent) -> None:
        context = self.context
        if context is None:
            return
        raw = context.state.get("quotes", self.quotes)
        self.quotes = int(str(raw)) + 1
        context.state["quotes"] = self.quotes
        if self.quotes % self.every:
            return
        side = Side.BUY if (self.quotes // self.every) % 2 else Side.SELL
        order_id = f"cycle-order-{self.quotes}"
        context.orders.submit(
            SingleLegOrder(
                event_id=order_id,
                timestamp=event.timestamp,
                sequence=event.sequence,
                order_id=order_id,
                symbol=event.symbol,
                side=side,
                quantity=self.quantity,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            )
        )


def _register_strategy_module() -> None:
    """Expose the test strategies under the configured implementation module."""
    module = types.ModuleType(STRATEGY_MODULE)
    module.__dict__["BuyAndHoldStrategy"] = BuyAndHoldStrategy
    module.__dict__["RestartCycleStrategy"] = RestartCycleStrategy
    sys.modules[STRATEGY_MODULE] = module


_register_strategy_module()


class MemorySnapshotStore:
    """In-memory adapter for the snapshot object-store port."""

    __slots__ = ("_objects",)

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def replace(self, relative_path: str, payload: bytes) -> None:
        self._objects[relative_path] = bytes(payload)

    def read(self, relative_path: str) -> bytes:
        try:
            return self._objects[relative_path]
        except KeyError:
            raise KeyError(relative_path) from None


def _quotes(count: int) -> list[QuoteEvent]:
    """Build one deterministic scripted quote stream for SPY."""
    return [
        QuoteEvent(
            event_id=f"quote-{index}",
            timestamp=OPEN + timedelta(minutes=index),
            sequence=index,
            symbol=Symbol("SPY"),
            bid_price=Decimal("449.99"),
            bid_quantity=Decimal("100"),
            ask_price=Decimal("450.01"),
            ask_quantity=Decimal("100"),
            venue="XNYS",
        )
        for index in range(1, count + 1)
    ]


def _options(**overrides: object) -> PaperOptions:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "initial_cash": CASH,
        "start": OPEN,
    }
    values.update(overrides)
    return PaperOptions(**cast(dict[str, Any], values))


def _trader_config(implementation: str) -> TraderConfig:
    example = load_example("trader")
    assert isinstance(example, TraderConfig)
    instance = StrategyInstanceConfig(
        instance_id=INSTANCE_ID,
        implementation=implementation,
        bundle_hash="b" * 64,
        capital_fraction=Decimal("1.0"),
        symbols=("SPY",),
        parameters={},
    )
    return example.model_copy(
        update={
            "mode": "paper",
            "seed": 7,
            "strategy_instances": (instance,),
        }
    )


def _state_store() -> RuntimeStateStore:
    return RuntimeStateStore(MemorySnapshotStore(), run_id=RUN_ID)


def _capture(
    shell: PaperShell, store: RuntimeStateStore, options: PaperOptions, sequence: int
) -> None:
    """Capture and save one snapshot exactly as the supervisor does."""
    snapshot = store.capture(
        shell,
        options=options,
        strategy_ids=(INSTANCE_ID,),
        trigger=SnapshotTrigger.PERIODIC,
        sequence=sequence,
    )
    store.save(snapshot)


def test_restored_shell_does_not_reenter_or_duplicate_restored_fill_ids() -> None:
    events = _quotes(4)
    config = _trader_config(f"{STRATEGY_MODULE}:BuyAndHoldStrategy")
    options = _options()
    store = _state_store()

    first = PaperShell(config, options=options)
    first.step(events[0])
    assert first.hub.book(INSTANCE_ID).fill_ids == ("sim-fill-3",)
    assert first.order_count(INSTANCE_ID) == 1

    _capture(first, store, options, sequence=events[0].sequence)

    second = store.restore_shell(store.load(), config)
    assert second.order_count(INSTANCE_ID) == 1
    assert second.venue_watermarks() == VenueWatermarks(id_ordinal=3, event_sequence=4)
    second.step(events[0])
    second.step(events[1])

    assert second.hub.snapshot == first.hub.snapshot
    assert second.hub.book(INSTANCE_ID).fill_ids == ("sim-fill-3",)
    assert second.order_count(INSTANCE_ID) == 1
    assert second.result().fills == first.result().fills

    # The supervision regression: a capture taken after the restore must
    # succeed — the books' fills are backed by the restored session history.
    _capture(second, store, options, sequence=events[1].sequence)


def test_restored_venue_ids_and_sequences_continue_past_restored_books() -> None:
    events = _quotes(12)
    split = 6
    config = _trader_config(f"{STRATEGY_MODULE}:RestartCycleStrategy")
    options = _options()
    store = _state_store()

    reference = PaperShell(config, options=options)
    for event in events:
        reference.step(event)
    expected = reference.result()

    first = PaperShell(config, options=options)
    for event in events[:split]:
        first.step(event)
    _capture(first, store, options, sequence=events[split - 1].sequence)

    snapshot = store.load()
    restored_fills = tuple(fill for book in snapshot.books for fill in book.fills)
    restored_ids = {fill_id for book in snapshot.books for fill_id in book.fill_ids}
    assert len(restored_ids) == 3

    second = store.restore_shell(snapshot, config)
    for event in events[split:]:
        second.step(event)
    result = second.result()

    assert result.fills == expected.fills
    restored_ordinals = [
        int(fill_id.rsplit("-", 1)[1])
        for fill_id in restored_ids
        if fill_id.startswith("sim-fill-")
    ]
    assert restored_ordinals
    new_fills = result.fills[len(restored_fills) :]
    assert new_fills
    assert all(
        int(fill.fill_id.rsplit("-", 1)[1]) > max(restored_ordinals)
        for fill in new_fills
    )
    assert all(
        fill.sequence > max(restored_fill.sequence for restored_fill in restored_fills)
        for fill in new_fills
    )
    assert second.order_count(INSTANCE_ID) == reference.order_count(INSTANCE_ID)
    assert second.hub.snapshot == reference.hub.snapshot
