"""Public strategy callback contract and its documented event ordering.

Bus-dispatched callbacks receive immutable market, order, and portfolio
events through the event bus.  ``on_corporate_action`` is engine-delivered
instead: the engine derives listing lifecycle events from the listings
catalog (delistings and renames) and calls the hook after the last
dispatched event of the event's knowledge date and before the first event
of any later date, in :meth:`harness.data.listing_events.CorporateActionEvent.sort_key`
order.  Engine machinery -- order cancellation on the delisted symbol,
pessimistic settlement, and rename migration -- is applied before delivery,
exactly as portfolio mutation precedes ``on_fill``.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from harness.core.types import (
    AccountSnapshot,
    Book,
    ExecutionReport,
    Fill,
    MultiLegOrder,
    Position,
    QuoteEvent,
    SingleLegOrder,
    TradeEvent,
)

if TYPE_CHECKING:
    from harness.data.listing_events import CorporateActionEvent


CALLBACK_ORDER = (
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


@runtime_checkable
class StrategyContext(Protocol):
    """The constrained capabilities a strategy observes through ``on_start``.

    The protocol is the plugin contract: whether the engine supplies the
    Python :class:`~harness.strategy.context.StrategyContext` (replay, paper,
    live) or the native Rust context handed across PyO3, a strategy only
    depends on this structural surface. Every member is duck-typed, so the
    engine can own a context natively without the strategy importing engine
    types.
    """

    @property
    def book_id(self) -> str:
        """The strategy's virtual book identifier."""
        ...

    @property
    def clock(self) -> Any:
        """The read-only clock capability (``now()``)."""
        ...

    @property
    def universe(self) -> Any:
        """The immutable canonical symbol universe."""
        ...

    @property
    def books(self) -> Any:
        """The read-only strategy book and account view."""
        ...

    @property
    def orders(self) -> Any:
        """The one-way order-submission boundary."""
        ...

    @property
    def data(self) -> Any:
        """The opaque, now-anchored point-in-time data capability."""
        ...

    @property
    def history(self) -> Any:
        """Alias for the point-in-time data capability."""
        ...

    @property
    def state(self) -> MutableMapping[str, object]:
        """Mutable strategy-local deterministic state."""
        ...

    @property
    def random(self) -> Any:
        """The per-context seeded random source."""
        ...

    @property
    def log(self) -> Any:
        """The structured logging sink."""
        ...

    @property
    def emissions(self) -> Any:
        """The structured emission sink."""
        ...

    def emit(
        self,
        name: str,
        fields: Mapping[str, object] | None = None,
        **values: object,
    ) -> None:
        """Emit one structured signal through the injected sink."""
        ...


class Strategy:
    """Base class for one mode-independent strategy implementation.

    Event callbacks receive the immutable event only. ``on_start`` supplies the
    constrained context, which strategies commonly retain as a private field.
    The same implementation can therefore be attached to replay, paper, or
    live adapters without changing strategy code.
    """

    callback_order: ClassVar[tuple[str, ...]] = CALLBACK_ORDER

    def on_start(self, context: StrategyContext) -> None:
        """Initialize strategy state before the first event."""

    def on_quote(self, event: QuoteEvent) -> None:
        """Handle a subscribed quote."""

    def on_trade(self, event: TradeEvent) -> None:
        """Handle a subscribed trade."""

    def on_order(self, event: SingleLegOrder | MultiLegOrder) -> None:
        """Handle a subscribed order event."""

    def on_execution_report(self, event: ExecutionReport) -> None:
        """Handle a subscribed execution report."""

    def on_fill(self, event: Fill) -> None:
        """Handle a subscribed fill after portfolio mutation."""

    def on_position(self, event: Position) -> None:
        """Handle a subscribed position snapshot."""

    def on_book(self, event: Book) -> None:
        """Handle a subscribed market-book event."""

    def on_account(self, event: AccountSnapshot) -> None:
        """Handle a subscribed account snapshot."""

    def on_corporate_action(
        self, event: CorporateActionEvent, context: StrategyContext
    ) -> None:
        """Handle one engine-delivered listing lifecycle event.

        ``event`` is a
        :class:`~harness.data.listing_events.DelistingEvent` or
        :class:`~harness.data.listing_events.RenameEvent`.  Delistings are
        delivered on their delisting date, renames on the successor symbol's
        first session, always after the engine has cancelled open orders on
        the delisted symbol, settled delisted positions pessimistically, and
        migrated renamed positions.  Strategies use ``context.orders`` and
        ``context.books`` here to observe or react to the settled state.
        """

    def on_stop(self, context: StrategyContext) -> None:
        """Finalize strategy state after the final event."""


__all__ = ["CALLBACK_ORDER", "Strategy", "StrategyContext"]
