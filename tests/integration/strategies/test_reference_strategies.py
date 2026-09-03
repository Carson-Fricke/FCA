"""Packaged-fixture integration tests for the reference equity strategies.

The fixture replays two symbols (SPY and QQQ) over two sessions of
deterministic quotes and prints through the real backtest runner.  SPY rises
during the first session and falls during the second; QQQ mirrors the pattern,
so momentum entries, directional exits, pre-close liquidations, mean-reversion
episodes with cancellations and replacements, and partial fills all occur.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from strategies.harness_examples import mean_reversion as mean_reversion_module

from harness.configuration import (
    BacktestConfig,
    ConfigurationValue,
    StrategyInstanceConfig,
    load_example,
)
from harness.core.symbols import Symbol
from harness.core.types import Fill, OrderStatus, QuoteEvent, Side, TradeEvent
from harness.runtime.backtest import (
    BacktestResult,
    BacktestStatus,
    StrategyLoader,
    run_backtest,
)
from harness.strategy import (
    Strategy,
    assert_sandbox_clean,
    lint_strategy_directory,
)

if TYPE_CHECKING:
    pass

REPO_ROOT = Path(__file__).resolve().parents[3]
SESSION_DATES = (date(2024, 1, 2), date(2024, 1, 3))
SESSION_OPENS = tuple(
    datetime.combine(day, time(13, 30), tzinfo=UTC) for day in SESSION_DATES
)
SESSION_CLOSES = tuple(
    datetime.combine(day, time(20, 0), tzinfo=UTC) for day in SESSION_DATES
)
LATE_SECOND = 23_100
QUOTE_QUANTITY = "100"
BUNDLE = "a" * 64

BUY_AND_HOLD_IMPLEMENTATION = (
    "strategies.harness_examples.buy_and_hold:BuyAndHoldStrategy"
)
MOMENTUM_IMPLEMENTATION = (
    "strategies.harness_examples.intraday_momentum:IntradayMomentumStrategy"
)
MEAN_REVERSION_IMPLEMENTATION = (
    "strategies.harness_examples.mean_reversion:MeanReversionStrategy"
)

_SPY_SESSIONS = (
    (
        ("quote", 0, "499.95", "500.05"),
        ("quote", 1, "500.05", "500.15"),
        ("quote", 2, "500.15", "500.25"),
        ("trade", 3, "500.20", None),
        ("quote", 4, "500.25", "500.35"),
        ("quote", 5, "500.35", "500.45"),
        ("quote", LATE_SECOND, "500.35", "500.45"),
        ("quote", LATE_SECOND + 1, "500.35", "500.45"),
    ),
    (
        ("quote", 0, "499.95", "500.05"),
        ("quote", 1, "499.85", "499.95"),
        ("trade", 2, "499.80", None),
        ("quote", 3, "499.75", "499.85"),
        ("quote", 4, "499.65", "499.75"),
        ("quote", 5, "499.55", "499.65"),
        ("quote", LATE_SECOND, "499.55", "499.65"),
        ("quote", LATE_SECOND + 1, "499.55", "499.65"),
    ),
)

_QQQ_SESSIONS = (
    (
        ("quote", 0, "399.95", "400.05"),
        ("quote", 1, "400.00", "400.10"),
        ("trade", 2, "400.10", None),
        ("quote", 3, "400.05", "400.15"),
        ("quote", 4, "400.10", "400.20"),
        ("quote", 5, "400.15", "400.25"),
        ("quote", LATE_SECOND, "400.15", "400.25"),
        ("quote", LATE_SECOND + 1, "400.15", "400.25"),
    ),
    (
        ("quote", 0, "399.95", "400.05"),
        ("quote", 1, "399.90", "400.00"),
        ("trade", 2, "399.85", None),
        ("quote", 3, "399.80", "399.90"),
        ("quote", 4, "399.75", "399.85"),
        ("quote", 5, "399.70", "399.80"),
        ("quote", LATE_SECOND, "399.70", "399.80"),
        ("quote", LATE_SECOND + 1, "399.70", "399.80"),
    ),
)


def _symbol_events(
    symbol: str,
    microsecond_offset: int,
    sessions: tuple[tuple[tuple[str, int, str, str | None], ...], ...],
) -> tuple[QuoteEvent | TradeEvent, ...]:
    events: list[QuoteEvent | TradeEvent] = []
    for session_index, rows in enumerate(sessions):
        for index, (kind, second, price_a, price_b) in enumerate(rows):
            stamp = SESSION_OPENS[session_index] + timedelta(
                seconds=second, microseconds=microsecond_offset
            )
            event_id = f"{symbol}-{kind}-{session_index}-{index}"
            canonical_symbol = Symbol(symbol)
            if kind == "quote":
                assert price_b is not None
                events.append(
                    QuoteEvent(
                        event_id,
                        stamp,
                        index + 10 * session_index,
                        canonical_symbol,
                        Decimal(price_a),
                        Decimal(QUOTE_QUANTITY),
                        Decimal(price_b),
                        Decimal(QUOTE_QUANTITY),
                    )
                )
            else:
                events.append(
                    TradeEvent(
                        event_id,
                        stamp,
                        index + 10 * session_index,
                        canonical_symbol,
                        Decimal(price_a),
                        Decimal(QUOTE_QUANTITY),
                    )
                )
    return tuple(events)


_SPY_EVENTS = _symbol_events("SPY", 0, _SPY_SESSIONS)
_QQQ_EVENTS = _symbol_events("QQQ", 400_000, _QQQ_SESSIONS)


def _streams() -> dict[object, object]:
    return {
        ("SPY", "quote"): tuple(
            event for event in _SPY_EVENTS if isinstance(event, QuoteEvent)
        ),
        ("SPY", "trade"): tuple(
            event for event in _SPY_EVENTS if isinstance(event, TradeEvent)
        ),
        ("QQQ", "quote"): tuple(
            event for event in _QQQ_EVENTS if isinstance(event, QuoteEvent)
        ),
        ("QQQ", "trade"): tuple(
            event for event in _QQQ_EVENTS if isinstance(event, TradeEvent)
        ),
    }


def _instance(
    instance_id: str,
    implementation: str,
    parameters: dict[str, ConfigurationValue],
    symbols: tuple[str, ...],
    capital_fraction: Decimal,
) -> StrategyInstanceConfig:
    return StrategyInstanceConfig(
        instance_id=instance_id,
        implementation=implementation,
        bundle_hash=BUNDLE,
        capital_fraction=capital_fraction,
        symbols=symbols,
        parameters=parameters,
    )


def _close_parameters() -> dict[str, ConfigurationValue]:
    return {
        "session_close_times": tuple(close.isoformat() for close in SESSION_CLOSES),
    }


def _buy_and_hold(capital_fraction: Decimal = Decimal("1")) -> StrategyInstanceConfig:
    return _instance(
        "buy-and-hold",
        BUY_AND_HOLD_IMPLEMENTATION,
        {"quantity": "25"},
        ("QQQ", "SPY"),
        capital_fraction,
    )


def _momentum(capital_fraction: Decimal = Decimal("1")) -> StrategyInstanceConfig:
    return _instance(
        "intraday-momentum",
        MOMENTUM_IMPLEMENTATION,
        {
            "lookback": 4,
            "threshold": "0.10",
            "quantity": "20",
            "liquidation_buffer_minutes": 10,
            **_close_parameters(),
        },
        ("QQQ", "SPY"),
        capital_fraction,
    )


def _mean_reversion(capital_fraction: Decimal = Decimal("1")) -> StrategyInstanceConfig:
    return _instance(
        "mean-reversion",
        MEAN_REVERSION_IMPLEMENTATION,
        {
            "reference_price": "500.00",
            "deviation_threshold": "0.25",
            "entry_quantity": "400",
            "resting_offset": "0.50",
            "liquidation_buffer_minutes": 30,
            "max_episodes": 1,
            **_close_parameters(),
        },
        ("SPY",),
        capital_fraction,
    )


def _configuration(
    instances: tuple[StrategyInstanceConfig, ...],
) -> BacktestConfig:
    base = load_example("backtest")
    assert isinstance(base, BacktestConfig)
    limits = {
        "max-order-notional": Decimal("250000.00"),
        "max-position-notional": Decimal("500000.00"),
    }
    rules = tuple(
        rule.model_copy(update={"parameters": {"limit": limits[rule.rule_id]}})
        for rule in base.risk_pipeline.rules
    )
    updates = {
        "run_id": "reference-strategies-integration",
        "start": SESSION_OPENS[0],
        "end": SESSION_CLOSES[-1],
        "data": base.data.model_copy(
            update={
                "snapshot_id": "integration-reference-strategies-v1",
                "symbols": ("QQQ", "SPY"),
            }
        ),
        "strategy_instances": instances,
        "risk_pipeline": base.risk_pipeline.model_copy(update={"rules": rules}),
    }
    return base.model_copy(update=updates)


def _loader() -> StrategyLoader:
    def load(instance: StrategyInstanceConfig) -> type[Strategy]:
        module_name, class_name = instance.implementation.rsplit(":", 1)
        found = getattr(import_module(module_name), class_name)
        assert isinstance(found, type) and issubclass(found, Strategy)
        return found

    return load


def _run(instances: tuple[StrategyInstanceConfig, ...]) -> BacktestResult:
    return run_backtest(
        _configuration(instances),
        data_source=_streams(),
        strategy_loader=_loader(),
    )


def _net_quantity(fills: tuple[Fill, ...], symbol: str, before: datetime) -> Decimal:
    total = Decimal("0")
    for fill in fills:
        if str(fill.symbol) == symbol and fill.timestamp < before:
            signed = fill.quantity if fill.side is Side.BUY else -fill.quantity
            total += signed
    return total


def test_reference_strategy_sources_are_sandbox_clean() -> None:
    directory_report = lint_strategy_directory(REPO_ROOT / "strategies")
    assert directory_report.ok, directory_report.render()

    for name in ("buy_and_hold.py", "intraday_momentum.py", "mean_reversion.py"):
        report = assert_sandbox_clean(
            REPO_ROOT / "strategies" / "harness_examples" / name
        )
        assert report.ok


def test_buy_and_hold_enters_one_position_per_symbol_and_holds() -> None:
    result = _run((_buy_and_hold(),))

    assert result.status is BacktestStatus.COMPLETED
    assert result.halted_strategies == ()
    assert result.stopped_strategies == ("buy-and-hold",)
    book = result.portfolio.book("buy-and-hold")
    assert book.fill_count == 2
    assert book.position("SPY").quantity == Decimal("25")
    assert book.position("QQQ").quantity == Decimal("25")
    assert {str(fill.symbol) for fill in result.fills} == {"SPY", "QQQ"}
    assert all(fill.side is Side.BUY for fill in result.fills)
    assert all(
        fill.timestamp < SESSION_CLOSES[0] - timedelta(minutes=30)
        for fill in result.fills
    )


def test_intraday_momentum_is_flat_at_every_session_close() -> None:
    result = _run((_momentum(),))

    assert result.status is BacktestStatus.COMPLETED
    assert result.halted_strategies == ()
    book = result.portfolio.book("intraday-momentum")
    assert book.fill_count == 8
    assert {fill.side for fill in result.fills} == {Side.BUY, Side.SELL}
    assert all(position.quantity == Decimal("0") for position in book.positions)
    for close in SESSION_CLOSES:
        for symbol in ("SPY", "QQQ"):
            assert _net_quantity(result.fills, symbol, close) == Decimal("0")


def test_intraday_momentum_artifact_is_identical_across_runs() -> None:
    first = _run((_momentum(),))
    second = _run((_momentum(),))

    assert first.completed
    assert second.completed
    assert first.artifact_json() == second.artifact_json()
    assert first.artifact_digest() == second.artifact_digest()


def test_mean_reversion_cancels_replaces_and_partially_fills() -> None:
    created = len(mean_reversion_module.INSTANCES)
    result = _run((_mean_reversion(),))

    assert result.status is BacktestStatus.COMPLETED
    assert result.halted_strategies == ()
    strategy = mean_reversion_module.INSTANCES[created]
    assert strategy.cancellation_count >= 1
    assert strategy.replacement_count >= 1
    assert strategy.partial_fill_count >= 1
    assert strategy.fill_count >= 2
    records = list(strategy.ledger.values())
    assert any(
        record.status == OrderStatus.CANCELED.value
        and Decimal("0") < record.filled_quantity < record.quantity
        for record in records
    )
    assert any(record.status == OrderStatus.REPLACED.value for record in records)
    book = result.portfolio.book("mean-reversion")
    assert book.fill_count == 2
    assert book.position("SPY").quantity == Decimal("-200")
    assert all(fill.quantity == Decimal("100") for fill in result.fills)


def test_all_three_reference_strategies_complete_one_run() -> None:
    result = _run(
        (
            _buy_and_hold(Decimal("0.34")),
            _momentum(Decimal("0.33")),
            _mean_reversion(Decimal("0.33")),
        )
    )

    assert result.status is BacktestStatus.COMPLETED
    assert result.halted_strategies == ()
    assert result.stopped_strategies == (
        "buy-and-hold",
        "intraday-momentum",
        "mean-reversion",
    )
    buy_and_hold = result.portfolio.book("buy-and-hold")
    assert buy_and_hold.position("SPY").quantity == Decimal("25")
    assert buy_and_hold.position("QQQ").quantity == Decimal("25")
    momentum = result.portfolio.book("intraday-momentum")
    assert all(position.quantity == Decimal("0") for position in momentum.positions)
    mean_reversion = result.portfolio.book("mean-reversion")
    assert mean_reversion.position("SPY").quantity == Decimal("-200")
