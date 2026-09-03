from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types import (
    AccountSnapshot,
    Book,
    ExecutionReport,
    ExecutionType,
    Fill,
    OrderStatus,
    OrderType,
    Position,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TradeEvent,
)
from harness.portfolio import PortfolioHub
from harness.strategy import (
    CALLBACK_ORDER,
    ReferenceStrategy,
    Strategy,
    StrategyContext,
    Subscription,
    bind_strategy,
)
from harness.venues import ExecutionVenue

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
AAPL = Symbol("AAPL")
SPY = Symbol("SPY")


class RecordingVenue:
    def __init__(self) -> None:
        self.submitted: list[SingleLegOrder] = []

    def submit(self, order: SingleLegOrder) -> None:
        self.submitted.append(order)

    def cancel(self, order_id: str) -> None:
        pass

    def replace(self, order_id: str, replacement: SingleLegOrder) -> None:
        pass

    def request_position_snapshot(self, account_id: str) -> None:
        pass


def _bus_and_context(
    *,
    books: PortfolioHub | None = None,
    book_id: str | None = None,
    venue: RecordingVenue | None = None,
    universe: tuple[Symbol, ...] = (AAPL,),
    subscriptions: tuple[Subscription, ...] | None = None,
) -> tuple[EventBus, StrategyContext, PortfolioHub]:
    clock = SimClock(STAMP)
    bus = EventBus(clock)
    hub = books or PortfolioHub("account")
    if not hub.book_ids:
        hub.create_book(book_id or "strategy", "1000")
    context = StrategyContext(
        clock,
        portfolio=hub,
        bus=bus,
        venue=cast(ExecutionVenue, venue),
        universe=universe,
        subscriptions=subscriptions,
        book_id=book_id,
        seed=1729,
    )
    return bus, context, hub


def _quote(event_id: str, symbol: Symbol = AAPL) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP,
        1,
        symbol,
        Decimal("99"),
        Decimal("1"),
        Decimal("101"),
        Decimal("1"),
    )


def _trade(event_id: str, symbol: Symbol = AAPL) -> TradeEvent:
    return TradeEvent(event_id, STAMP, 1, symbol, Decimal("100"), Decimal("1"))


def _order() -> SingleLegOrder:
    return SingleLegOrder(
        "order-event",
        STAMP,
        1,
        "order-1",
        AAPL,
        Side.BUY,
        Decimal("2"),
        OrderType.MARKET,
    )


def _report() -> ExecutionReport:
    return ExecutionReport(
        "report-event",
        STAMP,
        1,
        "report-1",
        "order-1",
        OrderStatus.FILLED,
        ExecutionType.FILL,
        Decimal("2"),
        Decimal("2"),
        Decimal("0"),
        Decimal("100"),
        Decimal("2"),
        Decimal("100"),
    )


def _fill() -> Fill:
    return Fill(
        "fill-event",
        STAMP,
        1,
        "fill-1",
        "order-1",
        AAPL,
        Side.BUY,
        Decimal("2"),
        Decimal("100"),
    )


def _position() -> Position:
    return Position(
        "position-event", STAMP, 1, "account", AAPL, Decimal("2"), Decimal("100")
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        "account-event",
        STAMP,
        1,
        "account",
        Decimal("800"),
        Decimal("1000"),
        Decimal("800"),
    )


def test_reference_strategy_receives_callbacks_in_documented_order() -> None:
    bus, context, _ = _bus_and_context(
        subscriptions=(
            Subscription(QuoteEvent),
            Subscription(TradeEvent),
            Subscription(SingleLegOrder),
            Subscription(ExecutionReport),
            Subscription(Fill),
            Subscription(Position),
            Subscription(Book),
            Subscription(AccountSnapshot),
        )
    )
    binding = bind_strategy(ReferenceStrategy, {}, context)
    binding.start()

    bus.publish_many(
        (
            _account(),
            Book("book-event", STAMP, 1, AAPL),
            _position(),
            _fill(),
            _report(),
            _order(),
            _trade("trade-event"),
            _quote("quote-event"),
        )
    )
    bus.drain()
    binding.stop()

    strategy = binding.strategy
    assert isinstance(strategy, ReferenceStrategy)
    assert strategy.callback_order == CALLBACK_ORDER
    assert strategy.callbacks == [
        "on_start",
        "on_quote",
        "on_trade",
        "on_order",
        "on_execution_report",
        "on_fill",
        "on_position",
        "on_book",
        "on_account",
        "on_stop",
    ]


class FillAwareStrategy(Strategy):
    def __init__(self) -> None:
        self.fill_observation: tuple[int, Decimal] | None = None

    def on_fill(self, event: Fill) -> None:
        context = self.context
        self.fill_observation = (
            context.books.current.fill_count,
            context.books.position(event.symbol).quantity,
        )

    def on_start(self, context: StrategyContext) -> None:
        self.context = context


def test_portfolio_mutates_immediately_before_on_fill() -> None:
    venue = RecordingVenue()
    bus, context, _ = _bus_and_context(
        venue=venue,
        subscriptions=(Subscription(Fill),),
    )
    binding = bind_strategy(FillAwareStrategy, {}, context)
    binding.start()
    bus.publish(_fill())
    bus.drain()

    actual = binding.strategy
    assert isinstance(actual, FillAwareStrategy)
    assert actual.fill_observation == (1, Decimal("2"))
    assert context.books.current.fill_count == 1


class EventRecorder(Strategy):
    def __init__(self, label: str) -> None:
        self.label = label
        self.received: list[str] = []

    def on_quote(self, event: QuoteEvent) -> None:
        self.received.append(f"quote:{event.symbol}")

    def on_trade(self, event: TradeEvent) -> None:
        self.received.append(f"trade:{event.symbol}")


def test_multiple_strategies_receive_only_their_subscribed_events() -> None:
    clock = SimClock(STAMP)
    bus = EventBus(clock)
    hub = PortfolioHub("account")
    hub.create_book("alpha", "1000")
    hub.create_book("beta", "1000")
    alpha_context = StrategyContext(
        clock,
        portfolio=hub,
        bus=bus,
        book_id="alpha",
        universe=(AAPL,),
        subscriptions=(Subscription(QuoteEvent, (AAPL,)),),
    )
    beta_context = StrategyContext(
        clock,
        portfolio=hub,
        bus=bus,
        book_id="beta",
        universe=(SPY,),
        subscriptions=(Subscription(TradeEvent, (SPY,)),),
    )
    alpha = bind_strategy(EventRecorder, {"label": "alpha"}, alpha_context)
    beta = bind_strategy(EventRecorder, {"label": "beta"}, beta_context)
    alpha.start()
    beta.start()

    bus.publish_many(
        (
            _trade("trade-aapl"),
            _quote("quote-aapl"),
            _quote("quote-spy", SPY),
            _trade("trade-spy", SPY),
        )
    )
    bus.drain()

    alpha_strategy = alpha.strategy
    beta_strategy = beta.strategy
    assert isinstance(alpha_strategy, EventRecorder)
    assert isinstance(beta_strategy, EventRecorder)
    assert alpha_strategy.received == ["quote:AAPL"]
    assert beta_strategy.received == ["trade:SPY"]


class ParameterStrategy(Strategy):
    def __init__(self, threshold: Decimal, symbol: str) -> None:
        self.threshold = threshold
        self.symbol = symbol


def test_strategy_binding_instantiates_class_from_parameters() -> None:
    bus, context, _ = _bus_and_context()

    binding = bind_strategy(
        ParameterStrategy,
        {"threshold": Decimal("0.015"), "symbol": "AAPL"},
        context,
    )

    assert isinstance(binding.strategy, ParameterStrategy)
    assert binding.strategy.threshold == Decimal("0.015")
    assert binding.strategy.symbol == "AAPL"
    assert not binding.started
    assert len(bus) == 0
